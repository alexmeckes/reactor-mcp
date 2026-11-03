import asyncio
from pathlib import Path

import httpx
import httpx2
import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Tests can only use explicitly injected mock transports; no real HTTP."""
    monkeypatch.delenv("REACTOR_API_KEY", raising=False)
    monkeypatch.setenv("REACTOR_ENABLE_LIVE", "false")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Real HTTP is prohibited in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", forbidden)


class FakeBackend:
    def __init__(self, key="test-only", seconds=60):
        self.key = key
        self.seconds = seconds
        self.status = "disconnected"
        self.session_id = None
        self.handlers = {}
        self.commands = []
        self.disconnected = 0
        self.closed = 0
        self.connect_gate = None
        self.capture_gate = None
        self.connect_error = None
        self.command_error = None
        self.disconnect_error = None
        self.reply = None

    def on(self, event, handler):
        self.handlers[event] = handler

    async def connect(self):
        self.status = "waiting"
        if self.connect_gate:
            await self.connect_gate.wait()
        if self.connect_error:
            raise self.connect_error
        self.status = "ready"
        self.session_id = "upstream-session-id"

    async def send_command(self, command, data):
        if self.command_error:
            raise self.command_error
        self.commands.append((command, data))
        return self.reply

    async def capture(self, seconds: float, target: Path):
        if self.capture_gate:
            await self.capture_gate.wait()
        target = target.with_suffix(".mp4")
        target.write_bytes(b"MOCK-MEDIA-NOT-A-REAL-VIDEO")
        return target, {"kind": "snap", "session_id": self.session_id}

    async def disconnect(self):
        self.disconnected += 1
        if self.disconnect_error:
            raise self.disconnect_error
        self.status = "disconnected"

    async def close(self):
        self.closed += 1
        self.status = "disconnected"


@pytest.fixture
def live_mock(monkeypatch, tmp_path):
    from reactor_mcp.service import Service

    monkeypatch.setenv("REACTOR_ENABLE_LIVE", "true")
    monkeypatch.setenv("REACTOR_API_KEY", "rk_test_only_not_a_real_secret")
    fake = FakeBackend()
    return Service(lambda key, seconds: fake, tmp_path), fake


async def ready(service):
    result = service.start("A quiet forest", 60)
    await asyncio.wait_for(service.sessions[result["handle"]].task, 1)
    return result["handle"]
