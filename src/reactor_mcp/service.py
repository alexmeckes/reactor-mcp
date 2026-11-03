"""Process-local session lifecycle. Handles below are not Reactor job IDs."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .backend import MODEL, LiveBackend, ReactorFailure, safe_error


@dataclass
class Session:
    handle: str
    backend: Any
    max_seconds: int
    phase: str = "connecting"
    created: float = field(default_factory=time.monotonic)
    upstream_id: str | None = None
    error: str | None = None
    cleanup: str | None = None
    model_state: dict = field(default_factory=dict)
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
        self.output_dir = (
            output_dir
            or Path(os.environ.get("REACTOR_OUTPUT_DIR", "outputs")).expanduser().resolve()
        )
        self.sessions: dict[str, Session] = {}
        self.captures: dict[str, Capture] = {}

    def describe(self) -> dict:
        return {
            "model": MODEL,
            "transport": "WebRTC via reactor-sdk 1.13.0",
            "tools": "start → status → set_prompt/control → capture → capture_status → close",
            "live_enabled": os.environ.get("REACTOR_ENABLE_LIVE") == "true",
            "api_key_configured": bool(os.environ.get("REACTOR_API_KEY", "").strip()),
            "max_concurrent_sessions": 1,
            "default_session_seconds": 60,
            "max_session_seconds": 300,
            "output_dir": str(self.output_dir),
            "limitations": [
                "Helios only. No live video player, audio, uploads, or other model schemas.",
                "Local handles and capture tasks disappear when this process exits.",
                "Billing starts when ready and continues while paused; close to end the session.",
                "Recording requires support enabled on the model. Keep generation running until capture completes.",
                "Clip media time differs from wall time; 120-second capture deadline, 256 MiB limit.",
            ],
            "docs": "https://docs.reactor.inc/model-api-reference/helios/schema",
        }

    def _session(self, handle: str) -> Session:
        if handle not in self.sessions:
            raise ReactorFailure(
                "UNKNOWN_SESSION: Use the handle returned by reactor_start in this process."
            )
        return self.sessions[handle]

    def status(self, handle: str) -> dict:
        session = self._session(handle)
        return {
            "handle": handle,
            "model": MODEL,
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
        }

    def start(self, prompt: str, max_seconds: int = 60) -> dict:
        if os.environ.get("REACTOR_ENABLE_LIVE") != "true":
            raise ReactorFailure(
                "LIVE_DISABLED: Enable REACTOR_ENABLE_LIVE=true only after approving billable use."
            )
        key = os.environ.get("REACTOR_API_KEY", "").strip()
        if not key or key == "replace-via-secure-local-secret-flow":
            raise ReactorFailure(
                "MISSING_API_KEY: Configure REACTOR_API_KEY securely in the server environment."
            )
        if not prompt.strip() or len(prompt) > 8000 or not 15 <= max_seconds <= 300:
            raise ReactorFailure(
                "INVALID_INPUT: Nonblank prompt (max 8000 chars), session duration 15–300 seconds."
            )
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
        backend = self.factory(key, max_seconds)
        session = Session(handle, backend, max_seconds)
        self.sessions[handle] = session
        backend.on("message", lambda message: self._message(session, message))
        backend.on("error", lambda error: setattr(session, "error", safe_error(error)))
        session.task = asyncio.create_task(self._start(session, prompt))
        session.timer = asyncio.create_task(self._expire(session))
        return self.status(handle)

    def _message(self, session: Session, message: Any):
        if not isinstance(message, dict):
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

    async def _send(self, session: Session, command: str, data: dict):
        reply = await asyncio.wait_for(session.backend.send_command(command, data), 15)
        if isinstance(reply, dict) and reply.get("type") == "command_error":
            raise ReactorFailure(
                "COMMAND_REJECTED: Helios rejected the command; inspect model_state."
            )

    async def _start(self, session: Session, prompt: str):
        try:
            await asyncio.wait_for(session.backend.connect(), 45)
            session.upstream_id = session.backend.session_id
            await self._send(session, "set_prompt", {"prompt": prompt})
            await self._send(session, "start", {})
            session.phase = "ready"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.error = safe_error(exc)
            await self.close(session.handle, failed=True)

    async def _expire(self, session: Session):
        await asyncio.sleep(session.max_seconds)
        await self.close(session.handle)

    async def command(self, handle: str, command: str, data: dict) -> dict:
        session = self._session(handle)
        async with session.lock:
            if session.phase != "ready" or session.backend.status != "ready":
                raise ReactorFailure(
                    "NOT_READY: Poll reactor_status until ready before sending commands."
                )
            try:
                await self._send(session, command, data)
            except Exception as exc:
                session.error = safe_error(exc)
                raise ReactorFailure(session.error) from None
        return {
            "acknowledged": True,
            "note": "Inspect model_state and error for asynchronous model events.",
            "session": self.status(handle),
        }

    def capture(self, handle: str, seconds: float) -> dict:
        session = self._session(handle)
        if session.phase != "ready" or session.backend.status != "ready":
            raise ReactorFailure("NOT_READY: Capture requires a ready session.")
        if not 0 < seconds <= 60:
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

    async def _capture(self, session: Session, capture: Capture, seconds: float):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            async with asyncio.timeout(120):
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
