"""Official Reactor SDK for sessions; bounded async HTTP for auth and clip files.

The SDK's downloader uses blocking urllib in a worker thread without per-request
timeouts. This small, cancellable HLS subset follows its 1.13.0 assembly contract.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from reactor_sdk import Reactor

MODEL = "reactor/helios"
API = "https://api.reactor.inc"
MAX_CLIP_BYTES = 256 * 1024 * 1024


class ReactorFailure(ToolError):
    """Safe, user-facing failure: never contains credentials or response bodies."""


def safe_error(exc: Exception) -> str:
    if isinstance(exc, ReactorFailure):
        return str(exc)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "TIMEOUT: Operation exceeded its deadline. Inspect status before retrying."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        hint = {
            401: "Check the API key and account access.",
            403: "Check model permissions and account access.",
            404: "The model or recording may be unavailable or expired.",
            409: "Inspect existing session state before retrying.",
            429: "Rate limited; wait before trying again.",
        }.get(status, "Reactor returned an unexpected HTTP response.")
        return f"HTTP_{status}: {hint}"
    code = getattr(exc, "code", None)
    hints = {
        "UNAUTHORIZED": "Check model permissions and credentials.",
        "RATE_LIMITED": "Rate limited; wait before trying again.",
        "CONFLICT": "Inspect existing session state before retrying.",
        "INVALID_STATE": "Wait for ready status before issuing this operation.",
        "NETWORK_ERROR": "Check network connectivity to Reactor.",
        "VERSION_MISMATCH": "The pinned SDK may need updating for the live service.",
    }
    if code in hints:
        return f"{code}: {hints[code]}"
    return "REACTOR_ERROR: Operation failed; check network, model access, and session status."


def checked_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ReactorFailure("INVALID_CLIP: Recording URL must be an HTTPS URL without user info.")
    return url


def auth_headers(url: str, jwt: str) -> dict[str, str]:
    parsed = urlsplit(checked_url(url))
    # Never attach a Reactor token to storage/CDN URLs or nonstandard ports.
    return (
        {"Authorization": f"Bearer {jwt}"}
        if (parsed.hostname == "api.reactor.inc" and parsed.port in (None, 443))
        else {}
    )


def parse_playlist(text: str, playlist_url: str) -> tuple[list[str], str]:
    """Accept a single unencrypted media playlist; refuse unsupported HLS variants."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ReactorFailure("INVALID_CLIP: Expected an HLS media playlist.")
    init = None
    segments = []
    for line in lines:
        if line.startswith(
            ("#EXT-X-STREAM-INF", "#EXT-X-KEY", "#EXT-X-BYTERANGE", "#EXT-X-DISCONTINUITY")
        ):
            raise ReactorFailure(
                "UNSUPPORTED_CLIP: Encrypted, master, ranged or discontinuous HLS."
            )
        if line.startswith("#EXT-X-MAP"):
            match = re.search(r'URI="([^"]+)"', line)
            if init is not None or not match or "BYTERANGE" in line:
                raise ReactorFailure("UNSUPPORTED_CLIP: Invalid or multiple HLS init segments.")
            init = match.group(1)
        elif not line.startswith("#"):
            segments.append(line)
    if not segments or len(segments) > 2000:
        raise ReactorFailure("INVALID_CLIP: Empty or oversized segment list.")
    if init is None and any(
        urlsplit(s).path.endswith((".m4s", ".mp4", ".m4a", ".m4v")) for s in segments
    ):
        raise ReactorFailure("INVALID_CLIP: Fragmented MP4 is missing its init segment.")
    urls = [checked_url(urljoin(playlist_url, s)) for s in ([init] if init else []) + segments]
    return urls, ".mp4" if init else ".ts"


async def download_clip(http: httpx.AsyncClient, clip, jwt: str, target: Path) -> Path:
    """Wait for Reactor's 202 readiness response, then stream init + fragments to disk.

    The caller owns the total deadline. HTTP requests also have a 15-second timeout.
    Redirects are deliberately refused so bearer tokens cannot leak via redirects.
    """
    url = checked_url(clip.playlist_url)
    while True:
        async with http.stream("GET", url, headers=auth_headers(url, jwt)) as response:
            if response.status_code != 202:
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 1024 * 1024:
                        raise ReactorFailure("INVALID_CLIP: Playlist exceeds 1 MiB.")
                urls, extension = parse_playlist(chunks.decode("utf-8"), url)
                break
            try:
                delay = min(2.0, max(0.2, float(response.headers.get("Retry-After", "2"))))
            except ValueError:
                delay = 2.0
        await asyncio.sleep(delay)
    partial = target.with_suffix(".part")
    final = target.with_suffix(extension)
    total = 0
    try:
        with partial.open("xb") as output:
            for segment in urls:
                async with http.stream(
                    "GET", segment, headers=auth_headers(segment, jwt)
                ) as response:
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise ReactorFailure("INVALID_CLIP: A segment is not ready or incomplete.")
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_CLIP_BYTES:
                            raise ReactorFailure("CLIP_TOO_LARGE: Clip exceeds the 256 MiB limit.")
                        output.write(chunk)
        if total == 0:
            raise ReactorFailure("INVALID_CLIP: Recording contained no bytes.")
        partial.replace(final)
        return final
    finally:
        partial.unlink(missing_ok=True)


class LiveBackend:
    def __init__(self, api_key: str, max_seconds: int):
        self.api_key = api_key
        self.max_seconds = max_seconds
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
        self.reactor = None
        self.jwt = None
        self.handlers = []

    @property
    def status(self):
        return self.reactor.status if self.reactor else "connecting"

    @property
    def session_id(self):
        return self.reactor.session_id if self.reactor else None

    def on(self, event, handler):
        self.handlers.append((event, handler))

    async def connect(self):
        response = await self.http.post(
            f"{API}/tokens",
            headers={"Reactor-API-Key": self.api_key},
            json={
                "authorization_details": [
                    {
                        "type": "session",
                        "resources": {"models": {"match": [MODEL]}},
                        "constraints": {
                            "max_sessions": 1,
                            "max_session_duration_seconds": self.max_seconds,
                        },
                    }
                ]
            },
        )
        response.raise_for_status()
        document = response.json()
        self.jwt = document.get("jwt") if isinstance(document, dict) else None
        if not isinstance(self.jwt, str) or not self.jwt:
            raise ReactorFailure("INVALID_AUTH_RESPONSE: Reactor returned no session token.")
        self.reactor = Reactor(model_name=MODEL, jwt=self.jwt)
        for event, handler in self.handlers:
            self.reactor.on(event, handler)
        await self.reactor.connect()

    async def send_command(self, command, data):
        return await self.reactor.send_command(command, data)

    async def capture(self, seconds: float, target: Path):
        clip = await self.reactor.request_clip(seconds)
        path = await download_clip(self.http, clip, self.jwt, target)
        return path, {
            "session_id": clip.session_id,
            "kind": clip.kind,
            "start_marker": clip.start_marker,
            "end_marker": clip.end_marker,
            "predicted_ready_at_ms": clip.predicted_ready_at_ms,
        }

    async def disconnect(self):
        if self.reactor:
            await self.reactor.disconnect()

    async def close(self):
        try:
            if self.reactor:
                self.reactor.close()
        finally:
            await self.http.aclose()
            self.jwt = None
            self.api_key = ""
