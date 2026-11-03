import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeBackend

from reactor_mcp import backend
from reactor_mcp.backend import (
    LiveBackend,
    ReactorFailure,
    download_clip,
    parse_playlist,
    safe_error,
)


async def test_scoped_auth_and_sdk_connection(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url == "https://api.reactor.inc/tokens"
        assert request.headers["Reactor-API-Key"] == "test-key"
        assert json.loads(request.content) == {
            "authorization_details": [
                {
                    "type": "session",
                    "resources": {"models": {"match": ["reactor/helios"]}},
                    "constraints": {"max_sessions": 1, "max_session_duration_seconds": 60},
                }
            ]
        }
        return httpx.Response(200, json={"jwt": "test-jwt"})

    fake = FakeBackend()

    def sdk_factory(**kwargs):
        assert kwargs == {"model_name": "reactor/helios", "jwt": "test-jwt"}
        # The real SDK has synchronous close(), unlike our service-facing test backend.
        fake.close = lambda: None
        return fake

    monkeypatch.setattr(backend, "Reactor", sdk_factory)
    live = LiveBackend("test-key", 60)
    await live.http.aclose()
    live.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    live.on("message", lambda _: None)
    await live.connect()
    assert live.status == "ready"
    assert live.session_id == "upstream-session-id"
    await live.send_command("start", {})
    await live.disconnect()
    await live.close()
    assert len(calls) == 1
    assert live.jwt is None and live.api_key == ""


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_http_errors_redacted(status):
    live = LiveBackend("test-key", 60)
    await live.http.aclose()
    live.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="secret-api-key-and-token")
        )
    )
    with pytest.raises(httpx.HTTPStatusError) as error:
        await live.connect()
    message = safe_error(error.value)
    assert f"HTTP_{status}" in message
    assert "secret" not in message
    await live.close()


async def test_missing_token():
    live = LiveBackend("test-key", 60)
    await live.http.aclose()
    live.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"unrelated": "secret"}))
    )
    with pytest.raises(ReactorFailure, match="INVALID_AUTH_RESPONSE"):
        await live.connect()
    await live.close()


async def test_clip_202_init_and_fragment_assembly(tmp_path):
    requests = []
    polls = 0

    def handler(request):
        nonlocal polls
        requests.append(request)
        if request.url.path == "/clip.m3u8":
            assert request.headers["Authorization"] == "Bearer test-jwt"
            polls += 1
            if polls == 1:
                return httpx.Response(202, headers={"Retry-After": "0"})
            return httpx.Response(
                200,
                text='#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
                "/chunk.m4s\nhttps://cdn.example.test/two.m4s?signed=1\n",
            )
        if request.url.host == "cdn.example.test":
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=b"TWO")
        assert request.headers["Authorization"] == "Bearer test-jwt"
        return httpx.Response(200, content=b"INIT" if request.url.path == "/init.mp4" else b"ONE")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        path = await download_clip(
            http,
            SimpleNamespace(playlist_url="https://api.reactor.inc/clip.m3u8"),
            "test-jwt",
            tmp_path / "capture",
        )
    assert path.suffix == ".mp4"
    assert path.read_bytes() == b"INITONETWO"
    assert polls == 2 and len(requests) == 5
    assert not list(tmp_path.glob("*.part"))


async def test_legacy_ts(tmp_path):
    def handler(request):
        return (
            httpx.Response(200, text="#EXTM3U\none.ts\n")
            if request.url.path.endswith("m3u8")
            else httpx.Response(200, content=b"TS")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        path = await download_clip(
            http,
            SimpleNamespace(playlist_url="https://api.reactor.inc/clip.m3u8"),
            "test-jwt",
            tmp_path / "capture",
        )
    assert path.suffix == ".ts" and path.read_bytes() == b"TS"


@pytest.mark.parametrize(
    "body",
    [
        "not HLS",
        "#EXTM3U",
        "#EXTM3U\none.m4s",
        "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128\none.ts",
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\none.m3u8",
        "#EXTM3U\nhttp://insecure.example/one.ts",
    ],
)
def test_invalid_or_unsupported_hls(body):
    with pytest.raises(ReactorFailure):
        parse_playlist(body, "https://api.reactor.inc/clip.m3u8")


async def test_failed_segment_and_size_limit_remove_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "MAX_CLIP_BYTES", 2)

    def handler(request):
        return (
            httpx.Response(200, text="#EXTM3U\none.ts\n")
            if request.url.path.endswith("m3u8")
            else httpx.Response(200, content=b"TOO LARGE")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ReactorFailure, match="CLIP_TOO_LARGE"):
            await download_clip(
                http,
                SimpleNamespace(playlist_url="https://api.reactor.inc/clip.m3u8"),
                "test-jwt",
                tmp_path / "capture",
            )
    assert not list(tmp_path.iterdir())


async def test_readiness_has_cancellable_deadline(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(202))
    ) as http:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                await download_clip(
                    http,
                    SimpleNamespace(playlist_url="https://api.reactor.inc/clip.m3u8"),
                    "test-jwt",
                    tmp_path / "capture",
                )
    assert not list(tmp_path.iterdir())


async def test_redirect_does_not_forward_token(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example.test/secret"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await download_clip(
                http,
                SimpleNamespace(playlist_url="https://api.reactor.inc/clip.m3u8"),
                "test-jwt",
                tmp_path / "capture",
            )
    assert len(requests) == 1


def test_real_sdk_surface_imports_without_network():
    import inspect

    from reactor_sdk import Reactor
    from reactor_sdk._ffi import get_lib

    assert get_lib() is not None  # Load the installed platform library, without creating a session.
    assert "jwt" in inspect.signature(Reactor).parameters
    for method in ["connect", "send_command", "request_clip", "disconnect", "close"]:
        assert callable(getattr(Reactor, method))
