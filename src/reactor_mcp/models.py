from typing import Any, Literal

from pydantic import BaseModel


class SessionStatus(BaseModel):
    handle: str
    model: str
    phase: Literal["connecting", "ready", "closing", "closed", "failed"]
    connection_status: str
    reactor_session_id: str | None
    remaining_seconds: int
    model_state: dict[str, bool | int]
    last_event: str | None
    error: str | None
    cleanup: str | None


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
