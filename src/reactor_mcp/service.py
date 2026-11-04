"""Process-local session lifecycle. Handles below are not Reactor job IDs."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import config
from .backend import HELIOS, OMNI, LiveBackend, ReactorFailure, safe_error

ASPECTS = {"16:9", "1:1", "9:16", "4:3"}
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_TYPES = {".wav", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac", ".webm"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
OMNI_STATE = {
    "playing",
    "autoplay",
    "flush_on_clip_end",
    "clip_seconds",
    "width",
    "height",
    "generation_queued",
    "generation_capacity",
    "playout_queued",
    "playout_capacity",
    "clips_played",
    "seconds_sent",
}
CLIP_PHASES = {
    "clip_queued": "queued",
    "clip_generated": "ready",
    "clip_started": "playing",
    "clip_finished": "finished",
    "clip_stopped": "stopped",
    "clip_popped": "popped",
    "clip_failed": "failed",
}
CODE = re.compile(r"[a-z][a-z_]{0,39}")


@dataclass
class Session:
    handle: str
    backend: Any
    max_seconds: int
    model: str = HELIOS
    phase: str = "connecting"
    created: float = field(default_factory=time.monotonic)
    upstream_id: str | None = None
    error: str | None = None
    cleanup: str | None = None
    model_state: dict = field(default_factory=dict)
    clips: dict[str, dict] = field(default_factory=dict)
    last_event: str | None = None
    task: asyncio.Task | None = None
    timer: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Capture:
    handle: str
    session_handle: str
    phase: str = "capturing"
    path: str | None = None
    metadata: dict | None = None
    error: str | None = None
    task: asyncio.Task | None = None


class Service:
    def __init__(self, factory: Callable = LiveBackend, output_dir: Path | None = None):
        self.factory = factory
        self.output_dir = output_dir or config.output_dir()
        self.sessions: dict[str, Session] = {}
        self.captures: dict[str, Capture] = {}

    def describe(self) -> dict:
        return {
            "models": {
                HELIOS: "start → status → set_prompt/control → capture → capture_status → close",
                OMNI: "omni_start → omni_enqueue → status (clips) → omni_control → "
                "capture → capture_status → close",
            },
            "transport": "WebRTC via reactor-sdk 1.13.0",
            "live_enabled": config.live_enabled(),
            "api_key_configured": config.key_configured(),
            "max_concurrent_sessions": 1,
            "default_session_seconds": 60,
            "max_session_seconds": 300,
            "output_dir": str(self.output_dir),
            "limitations": [
                "Helios and FastH3-Omni only. No live player; captures are how you see output.",
                "Omni clips reach the stream, and so a capture, only once they play.",
                "Uploads are absolute local image/audio paths, at most 25 MiB, sent to Reactor.",
                "Local handles and capture tasks disappear when this process exits.",
                "Billing starts when ready and continues while paused; close to end the session.",
                "Recording requires support enabled on the model. Keep generation running until capture completes.",
                "Clip media time differs from wall time; 120-second capture deadline "
                "(300 for a full session), 256 MiB limit.",
            ],
            "docs": [
                "https://docs.reactor.inc/model-api-reference/helios/schema",
                "https://docs.reactor.inc/model-api-reference/fast-h3-omni/schema",
                "https://docs.reactor.inc/model-api-reference/fast-h3-omni/prompt-guide",
            ],
        }

    def _session(self, handle: str, model: str | None = None) -> Session:
        if handle not in self.sessions:
            raise ReactorFailure(
                "UNKNOWN_SESSION: Use the handle returned by reactor_start in this process."
            )
        session = self.sessions[handle]
        if model and session.model != model:
            raise ReactorFailure(f"WRONG_MODEL: This handle is a {session.model} session.")
        return session

    def status(self, handle: str) -> dict:
        session = self._session(handle)
        return {
            "handle": handle,
            "model": session.model,
            "phase": session.phase,
            "connection_status": session.backend.status,
            "reactor_session_id": session.upstream_id or session.backend.session_id,
            "remaining_seconds": max(
                0, round(session.max_seconds - (time.monotonic() - session.created))
            ),
            "model_state": session.model_state,
            "last_event": session.last_event,
            "error": session.error,
            "cleanup": session.cleanup,
            "clips": list(session.clips.values()),
        }

    def _key(self) -> str:
        if not config.live_enabled():
            raise ReactorFailure(
                "LIVE_DISABLED: Run reactor-mcp setup or set REACTOR_ENABLE_LIVE=true after approving billable use."
            )
        try:
            key = config.api_key()
        except config.ConfigError as exc:
            raise ReactorFailure(f"CREDENTIALS_UNAVAILABLE: {exc}") from None
        if not key or key == "replace-via-secure-local-secret-flow":
            raise ReactorFailure(
                "MISSING_API_KEY: Run reactor-mcp setup or supply REACTOR_API_KEY securely in the server environment."
            )
        return key

    def start(self, prompt: str, max_seconds: int = 60) -> dict:
        key = self._key()
        if not prompt.strip() or len(prompt) > 8000 or not 15 <= max_seconds <= 300:
            raise ReactorFailure(
                "INVALID_INPUT: Nonblank prompt (max 8000 chars), session duration 15–300 seconds."
            )
        return self._open(
            key, HELIOS, max_seconds, [("set_prompt", {"prompt": prompt}), ("start", {})]
        )

    def start_omni(
        self,
        max_seconds: int = 120,
        aspect: str = "16:9",
        autoplay: bool = True,
        flush_on_clip_end: bool = True,
    ) -> dict:
        key = self._key()
        if aspect not in ASPECTS or not 15 <= max_seconds <= 300:
            raise ReactorFailure(
                "INVALID_INPUT: Aspect 16:9, 1:1, 9:16 or 4:3; session duration 15–300 seconds."
            )
        # The canvas can only change while the queues are empty, so it is set before any clip.
        setup = [
            ("set_canvas", {"aspect": aspect}),
            ("set_autoplay", {"enabled": autoplay}),
            ("set_flush_on_clip_end", {"enabled": flush_on_clip_end}),
        ]
        return self._open(key, OMNI, max_seconds, setup)

    def _open(self, key: str, model: str, max_seconds: int, setup: list[tuple[str, dict]]) -> dict:
        if any(
            s.phase not in ("closed", "failed")
            or (s.cleanup == "uncertain" and time.monotonic() - s.created < s.max_seconds + 45)
            for s in self.sessions.values()
        ):
            raise ReactorFailure(
                "SESSION_LIMIT: Close the existing session first; uncertain cleanup waits for the server cap."
            )
        # Bound process history; artifacts remain on disk under their generated names.
        if len(self.sessions) >= 20:
            self.sessions.pop(next(iter(self.sessions)))
        handle = uuid4().hex
        backend = self.factory(key, max_seconds, model)
        session = Session(handle, backend, max_seconds, model)
        self.sessions[handle] = session
        backend.on("message", lambda message: self._message(session, message))
        backend.on("error", lambda error: setattr(session, "error", safe_error(error)))
        session.task = asyncio.create_task(self._start(session, setup))
        session.timer = asyncio.create_task(self._expire(session))
        return self.status(handle)

    def _message(self, session: Session, message: Any):
        if not isinstance(message, dict):
            return
        if session.model == OMNI:
            self._omni_message(session, message)
            return
        event = message.get("type")
        if event in {
            "state",
            "generation_started",
            "generation_paused",
            "generation_resumed",
            "chunk_complete",
            "prompt_accepted",
            "command_error",
        }:
            session.last_event = event
        if event == "state" and isinstance(message.get("data"), dict):
            # Only small diagnostic fields; no raw prompts, arbitrary messages, tokens or URLs.
            session.model_state = {
                k: v
                for k, v in message["data"].items()
                if k in {"running", "started", "paused", "current_chunk", "current_frame"}
                and type(v) in (bool, int)
            }
        if event == "command_error":
            session.error = (
                "COMMAND_REJECTED: Helios rejected a command; inspect model_state before retrying."
            )

    def _omni_message(self, session: Session, message: dict) -> str | None:
        """Track queue state and per-clip progress; returns the clip ID a message names."""
        event = message.get("type")
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        if event in CLIP_PHASES or event in {
            "state_update",
            "queue_update",
            "clip_moved",
            "session_reset",
            "command_error",
        }:
            # Omni refusals are raised to the caller; the command_error broadcast is not an error.
            session.last_event = event
        if event == "state_update":
            session.model_state = {
                k: v for k, v in data.items() if k in OMNI_STATE and type(v) in (bool, int, float)
            }
        if event == "session_reset":
            for clip in session.clips.values():
                if clip["phase"] in ("queued", "ready", "playing"):
                    clip["phase"] = "cleared"
        if event not in CLIP_PHASES and event != "clip_moved":
            return None
        clip = data.get("clip")
        clip_id = clip.get("clip_id") if isinstance(clip, dict) else None
        if not isinstance(clip_id, str) or not re.fullmatch(r"[0-9A-Za-z-]{1,64}", clip_id):
            return None
        entry = session.clips.get(clip_id)
        if entry is None:
            if len(session.clips) >= 50:
                session.clips.pop(next(iter(session.clips)))
            entry = session.clips[clip_id] = dict.fromkeys(
                ("seconds", "build_seconds", "continue_mode", "metadata", "reason")
            )
            entry.update(clip_id=clip_id, phase="queued")
        elif event == "clip_queued":
            # A late enqueue reply is older than the broadcasts already applied.
            return clip_id
        if event != "clip_moved":
            entry["phase"] = CLIP_PHASES[event]
        # Only known fields are kept: prompts are not echoed, metadata is the caller's own label.
        metadata = clip.get("metadata")
        if isinstance(metadata, str) and metadata:
            entry["metadata"] = metadata[:200]
        if clip.get("continue_mode") in ("latent", "frame"):
            entry["continue_mode"] = clip["continue_mode"]
        for key in ("seconds", "build_seconds"):
            if type(clip.get(key)) in (int, float):
                entry[key] = float(clip[key])
        if event == "clip_failed":
            reason = data.get("reason")
            entry["reason"] = (
                reason if isinstance(reason, str) and CODE.fullmatch(reason) else "unspecified"
            )
        return clip_id

    async def _send(self, session: Session, command: str, data: dict):
        reply = await asyncio.wait_for(session.backend.send_command(command, data), 15)
        if isinstance(reply, dict) and reply.get("type") == "command_error":
            raise ReactorFailure(
                "COMMAND_REJECTED: The model rejected the command; inspect model_state."
            )
        return reply

    async def _start(self, session: Session, setup: list[tuple[str, dict]]):
        try:
            await asyncio.wait_for(session.backend.connect(), 45)
            session.upstream_id = session.backend.session_id
            for command, data in setup:
                await self._send(session, command, data)
            session.phase = "ready"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.error = safe_error(exc)
            await self.close(session.handle, failed=True)

    async def _expire(self, session: Session):
        await asyncio.sleep(session.max_seconds)
        await self.close(session.handle)

    async def command(
        self,
        handle: str,
        command: str,
        data: dict,
        *,
        model: str = HELIOS,
        uploads: dict[str, Path | list[Path]] | None = None,
    ) -> dict:
        session = self._session(handle, model)
        async with session.lock:
            if session.phase != "ready" or session.backend.status != "ready":
                raise ReactorFailure(
                    "NOT_READY: Poll reactor_status until ready before sending commands."
                )
            try:
                for key, value in (uploads or {}).items():
                    refs = [
                        await asyncio.wait_for(session.backend.upload(path), 60)
                        for path in (value if isinstance(value, list) else [value])
                    ]
                    data[key] = refs if isinstance(value, list) else refs[0]
                reply = await self._send(session, command, data)
            except Exception as exc:
                session.error = safe_error(exc)
                raise ReactorFailure(session.error) from None
        # Omni replies carry the clip they acted on, so they update clip state like broadcasts.
        clip_id = self._omni_message(session, reply) if model == OMNI and reply else None
        return {
            "acknowledged": True,
            "note": "Inspect model_state, clips and error for asynchronous model events.",
            "session": self.status(handle),
            "clip_id": clip_id,
        }

    @staticmethod
    def _media(path: str, kinds: set[str]) -> Path:
        # Uploads leave this machine: only regular files of the model's media types qualify.
        file = Path(path).expanduser()
        resolved = file.resolve()
        if (
            not file.is_absolute()
            or resolved.suffix.lower() not in kinds
            or not resolved.is_file()
            or resolved.stat().st_size > MAX_UPLOAD_BYTES
        ):
            raise ReactorFailure(
                f"INVALID_INPUT: Uploads must be absolute paths to existing "
                f"{', '.join(sorted(kinds))} files of at most 25 MiB."
            )
        return resolved

    async def enqueue(
        self,
        handle: str,
        prompt: str,
        *,
        reference_images: Sequence[str] = (),
        reference_audios: Sequence[str] = (),
        first_frame_image: str | None = None,
        last_frame_image: str | None = None,
        continue_from_clip_id: str | None = None,
        continue_mode: str | None = None,
        seconds: float | None = None,
        seed: int | None = None,
        position: int | None = None,
        metadata: str = "",
    ) -> dict:
        if not prompt.strip() or len(prompt) > 8000:
            raise ReactorFailure("INVALID_INPUT: Nonblank prompt, at most 8000 characters.")
        uploads = {
            "reference_images": [self._media(p, IMAGE_TYPES) for p in reference_images],
            "reference_audios": [self._media(p, AUDIO_TYPES) for p in reference_audios],
            "first_frame_image": first_frame_image and self._media(first_frame_image, IMAGE_TYPES),
            "last_frame_image": last_frame_image and self._media(last_frame_image, IMAGE_TYPES),
        }
        options = {
            "continue_from_clip_id": continue_from_clip_id,
            "continue_mode": continue_mode,
            "seconds": seconds,
            "seed": seed,
            "position": position,
        }
        data = {"prompt": prompt, "metadata": metadata}
        data.update({k: v for k, v in options.items() if v is not None})
        return await self.command(
            handle,
            "enqueue",
            data,
            model=OMNI,
            uploads={k: v for k, v in uploads.items() if v},
        )

    async def omni_control(
        self, handle: str, action: str, clip_id: str | None = None, position: int | None = None
    ) -> dict:
        if (action in ("pop", "move") and not clip_id) or (action == "move" and position is None):
            raise ReactorFailure("INVALID_INPUT: pop and move need clip_id; move needs position.")
        data = {"clip_id": clip_id} if clip_id and action in ("play", "pop", "move") else {}
        if action == "move":
            data["position"] = position
        return await self.command(handle, action, data, model=OMNI)

    def capture(self, handle: str, seconds: float | None) -> dict:
        """Capture the last N media seconds, or the whole session when seconds is None."""
        session = self._session(handle)
        if session.phase != "ready" or session.backend.status != "ready":
            raise ReactorFailure("NOT_READY: Capture requires a ready session.")
        if seconds is not None and not 0 < seconds <= 60:
            raise ReactorFailure(
                "INVALID_INPUT: Clip duration must be greater than 0 and at most 60 seconds."
            )
        if any(c.phase == "capturing" for c in self.captures.values()):
            raise ReactorFailure("CAPTURE_LIMIT: Wait for the current capture to finish.")
        if len(self.captures) >= 20:
            self.captures.pop(next(iter(self.captures)))
        capture = Capture(uuid4().hex, handle)
        self.captures[capture.handle] = capture
        capture.task = asyncio.create_task(self._capture(session, capture, seconds))
        return self.capture_status(capture.handle)

    async def _capture(self, session: Session, capture: Capture, seconds: float | None):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            async with asyncio.timeout(120 if seconds is not None else 300):
                path, metadata = await session.backend.capture(
                    seconds, self.output_dir / capture.handle
                )
            capture.path = str(path)
            capture.metadata = metadata
            capture.phase = "complete"
        except asyncio.CancelledError:
            capture.phase = "cancelled"
            capture.error = (
                "CAPTURE_CANCELLED: Session closed or server exited before capture completed."
            )
            raise
        except Exception as exc:
            capture.phase = "failed"
            capture.error = safe_error(exc)

    def capture_status(self, handle: str) -> dict:
        if handle not in self.captures:
            raise ReactorFailure(
                "UNKNOWN_CAPTURE: Use the handle returned by reactor_capture in this process."
            )
        capture = self.captures[handle]
        return {
            "capture_handle": handle,
            "session_handle": capture.session_handle,
            "phase": capture.phase,
            "path": capture.path,
            "metadata": capture.metadata,
            "error": capture.error,
        }

    async def close(self, handle: str, *, failed: bool = False) -> dict:
        session = self._session(handle)
        if session.cleanup_task is None:
            session.cleanup_task = asyncio.create_task(
                self._close(session, failed, asyncio.current_task())
            )
        # A cancelled MCP request must not cancel GPU cleanup. Concurrent closes share it.
        return await asyncio.shield(session.cleanup_task)

    async def _close(self, session: Session, failed: bool, origin: asyncio.Task | None) -> dict:
        handle = session.handle
        # Cancel startup before taking the lock so a slow connection cannot block close.
        for task in (session.task, session.timer):
            if task and task is not origin and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        async with session.lock:
            if session.phase in ("closed", "failed"):
                return self.status(handle)
            session.phase = "closing"
            for capture in self.captures.values():
                if capture.session_handle == handle and capture.task and not capture.task.done():
                    capture.task.cancel()
                    with suppress(asyncio.CancelledError):
                        await capture.task
                    # A task cancelled before its first tick never enters its try/finally.
                    capture.phase = "cancelled"
                    capture.error = "CAPTURE_CANCELLED: Session closed before capture completed."
            session.upstream_id = session.upstream_id or session.backend.session_id
            try:
                await asyncio.wait_for(session.backend.disconnect(), 10)
                session.cleanup = "disconnect_acknowledged"
            except Exception:
                session.cleanup = "uncertain"
                session.error = "CLEANUP_UNCERTAIN: Disconnect failed; server-side duration cap remains the fallback."
            finally:
                try:
                    await asyncio.wait_for(session.backend.close(), 5)
                except Exception:
                    session.cleanup = "uncertain"
                session.phase = "failed" if failed else "closed"
        return self.status(handle)

    async def shutdown(self):
        for handle in list(self.sessions):
            await self.close(handle)
