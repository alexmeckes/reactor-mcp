import asyncio
import json

import pytest
from conftest import ready
from reactor_sdk.errors import RateLimitedError

from reactor_mcp.backend import ReactorFailure
from reactor_mcp.service import Service


async def test_live_gate_and_missing_key(monkeypatch):
    service = Service(lambda *_: pytest.fail("Must not construct a backend"))
    with pytest.raises(ReactorFailure, match="LIVE_DISABLED"):
        service.start("forest")
    monkeypatch.setenv("REACTOR_ENABLE_LIVE", "true")
    with pytest.raises(ReactorFailure, match="MISSING_API_KEY"):
        service.start("forest")


async def test_complete_workflow(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    assert fake.commands == [("set_prompt", {"prompt": "A quiet forest"}), ("start", {})]
    assert service.status(handle)["phase"] == "ready"
    await service.command(handle, "set_prompt", {"prompt": "A rainy forest"})
    await service.command(handle, "pause", {})
    await service.command(handle, "resume", {})
    fake.handlers["message"](
        {"type": "state", "data": {"running": True, "current_frame": 33, "unexpected": "secret"}}
    )
    assert service.status(handle)["model_state"] == {"running": True, "current_frame": 33}
    capture = service.capture(handle, 5)
    await service.captures[capture["capture_handle"]].task
    result = service.capture_status(capture["capture_handle"])
    assert result["phase"] == "complete"
    assert result["path"].endswith(".mp4")
    assert (await service.close(handle))["cleanup"] == "disconnect_acknowledged"
    await service.close(handle)
    assert fake.disconnected == fake.closed == 1


async def test_start_does_not_block_and_close_cancels_connect(live_mock):
    service, fake = live_mock
    fake.connect_gate = asyncio.Event()
    result = service.start("forest")
    assert result["phase"] == "connecting"
    with pytest.raises(ReactorFailure, match="NOT_READY"):
        await service.command(result["handle"], "pause", {})
    await asyncio.sleep(0)
    await asyncio.wait_for(service.close(result["handle"]), 1)
    assert service.sessions[result["handle"]].task.cancelled()
    assert fake.closed == 1


async def test_start_failure_cleans_up_and_sanitizes(live_mock):
    service, fake = live_mock
    fake.connect_error = RuntimeError("rk_test_only_not_a_real_secret jwt-do-not-output")
    handle = await ready(service)
    result = service.status(handle)
    assert result["phase"] == "failed"
    assert "jwt-do-not-output" not in json.dumps(result)
    assert "rk_test" not in json.dumps(result)
    assert fake.closed == fake.disconnected == 1


async def test_command_refusal_and_async_event(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    fake.reply = {"type": "command_error", "data": {"reason": "secret"}}
    with pytest.raises(ReactorFailure, match="COMMAND_REJECTED"):
        await service.command(handle, "pause", {})
    fake.handlers["message"](fake.reply)
    assert service.status(handle)["last_event"] == "command_error"
    assert "secret" not in service.status(handle)["error"]
    await service.shutdown()


async def test_rate_limit_is_visible_no_retry(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    fake.command_error = RateLimitedError("secret response", retry_after_ms=1000)
    with pytest.raises(ReactorFailure, match="RATE_LIMITED"):
        await service.command(handle, "pause", {})
    assert len(fake.commands) == 2
    await service.shutdown()


async def test_limits_and_capture_cancellation(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    with pytest.raises(ReactorFailure, match="SESSION_LIMIT"):
        service.start("second session")
    fake.capture_gate = asyncio.Event()
    capture = service.capture(handle, 5)
    with pytest.raises(ReactorFailure, match="CAPTURE_LIMIT"):
        service.capture(handle, 5)
    await asyncio.sleep(0)
    await service.shutdown()
    assert service.capture_status(capture["capture_handle"])["phase"] == "cancelled"
    assert fake.closed == 1


async def test_disconnect_failure_is_uncertain(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    fake.disconnect_error = RuntimeError("network disconnected")
    result = await service.close(handle)
    assert result["cleanup"] == "uncertain"
    assert result["error"].startswith("CLEANUP_UNCERTAIN")
    assert fake.closed == 1
    with pytest.raises(ReactorFailure, match="SESSION_LIMIT"):
        service.start("second session")


async def test_automatic_expiry(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    session = service.sessions[handle]
    session.timer.cancel()
    session.max_seconds = 0
    await service._expire(session)
    assert service.status(handle)["phase"] == "closed"
    assert fake.closed == 1


@pytest.mark.parametrize("prompt,seconds", [(" ", 60), ("x" * 8001, 60), ("x", 301), ("x", 0)])
async def test_validation(live_mock, prompt, seconds):
    service, _ = live_mock
    with pytest.raises(ReactorFailure, match="INVALID_INPUT"):
        service.start(prompt, seconds)


async def test_unknown_handles(live_mock):
    service, _ = live_mock
    with pytest.raises(ReactorFailure, match="UNKNOWN_SESSION"):
        service.status("a" * 32)
    with pytest.raises(ReactorFailure, match="UNKNOWN_CAPTURE"):
        service.capture_status("a" * 32)


async def test_immediate_capture_cancellation(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    fake.capture_gate = asyncio.Event()
    capture = service.capture(handle, 5)
    await service.close(handle)
    assert service.capture_status(capture["capture_handle"])["phase"] == "cancelled"


async def test_concurrent_close_is_single_cleanup(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    results = await asyncio.wait_for(
        asyncio.gather(
            service.close(handle),
            service.close(handle),
            service.shutdown(),
        ),
        1,
    )
    assert results[0]["phase"] == results[1]["phase"] == "closed"
    assert fake.closed == fake.disconnected == 1


async def test_cancelled_close_caller_does_not_cancel_cleanup(live_mock):
    service, fake = live_mock
    handle = await ready(service)
    gate = asyncio.Event()

    async def slow_disconnect():
        await gate.wait()
        fake.disconnected += 1

    fake.disconnect = slow_disconnect
    closer = asyncio.create_task(service.close(handle))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    gate.set()
    await asyncio.wait_for(service.shutdown(), 1)
    assert fake.closed == fake.disconnected == 1
