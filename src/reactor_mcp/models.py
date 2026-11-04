from typing import Any, Literal

from pydantic import BaseModel


class OmniClip(BaseModel):
    clip_id: str
    phase: Literal[
        "queued", "ready", "playing", "finished", "stopped", "popped", "failed", "cleared"
    ]
    seconds: float | None
    build_seconds: float | None
    continue_mode: str | None
    metadata: str | None
    reason: str | None


class SessionStatus(BaseModel):
    handle: str
    model: str
    phase: Literal["connecting", "ready", "closing", "closed", "failed"]
    connection_status: str
    reactor_session_id: str | None
    remaining_seconds: int
    model_state: dict[str, bool | int | float]
    last_event: str | None
    error: str | None
    cleanup: str | None
    clips: list[OmniClip] = []


class CaptureStatus(BaseModel):
    capture_handle: str
    session_handle: str
    phase: Literal["capturing", "complete", "failed", "cancelled"]
    path: str | None
    metadata: dict[str, Any] | None
    error: str | None


class CommandResult(BaseModel):
    acknowledged: bool
    note: str
    session: SessionStatus
    clip_id: str | None = None
