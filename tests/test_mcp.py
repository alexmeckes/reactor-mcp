import asyncio
import os
import sys

import pytest
from mcp import Client, StdioServerParameters

from reactor_mcp.server import create_server


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_discovery_validation_and_full_mock_workflow(live_mock, mode):
    service, fake = live_mock
    server = create_server(service)
    async with Client(server, mode=mode) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == 8
        assert all(t.input_schema and t.output_schema for t in tools)
        start_tool = next(t for t in tools if t.name == "reactor_start")
        assert start_tool.input_schema["properties"]["max_seconds"]["maximum"] == 300
        assert not start_tool.annotations.read_only_hint
        assert start_tool.annotations.open_world_hint
        assert not start_tool.annotations.idempotent_hint
        close_tool = next(t for t in tools if t.name == "reactor_close")
        assert close_tool.annotations.destructive_hint
        assert close_tool.annotations.idempotent_hint
        status_tool = next(t for t in tools if t.name == "reactor_status")
        assert status_tool.annotations.read_only_hint
        assert not status_tool.annotations.open_world_hint
        for args in [{"prompt": " "}, {"prompt": "forest", "max_seconds": 999}]:
            result = await client.call_tool("reactor_start", args)
            assert result.is_error
        result = await client.call_tool("reactor_start", {"prompt": "forest"})
        assert not result.is_error
        handle = result.structured_content["handle"]
        await service.sessions[handle].task
        status = await client.call_tool("reactor_status", {"handle": handle})
        assert status.structured_content["phase"] == "ready"
        changed = await client.call_tool(
            "reactor_set_prompt", {"handle": handle, "prompt": "ocean"}
        )
        assert changed.structured_content["acknowledged"]
        invalid = await client.call_tool("reactor_control", {"handle": handle, "action": "delete"})
        assert invalid.is_error
        clip = await client.call_tool("reactor_capture", {"handle": handle, "seconds": 3})
        capture_handle = clip.structured_content["capture_handle"]
        await service.captures[capture_handle].task
        result = await client.call_tool(
            "reactor_capture_status", {"capture_handle": capture_handle}
        )
        assert result.structured_content["phase"] == "complete"
        result = await client.call_tool("reactor_close", {"handle": handle})
        assert result.structured_content["phase"] == "closed"
    assert fake.closed == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_stdio_process_discovery_and_safe_default(mode):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "reactor_mcp.server"],
        env={"PATH": os.environ.get("PATH", ""), "REACTOR_ENABLE_LIVE": "false"},
    )
    async with asyncio.timeout(15):
        async with Client(params, mode=mode) as client:
            assert client.server_info.name == "Reactor inference"
            assert client.server_info.version == "0.3.0"
            assert client.protocol_version == ("2025-11-25" if mode == "legacy" else "2026-07-28")
            assert len((await client.list_tools()).tools) == 8
            info = await client.call_tool("reactor_describe", {})
            assert not info.structured_content["live_enabled"]
            result = await client.call_tool("reactor_start", {"prompt": "forest"})
            assert result.is_error
            assert "LIVE_DISABLED" in result.content[0].text


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_client_exit_closes_unfinished_session(live_mock, mode):
    service, fake = live_mock
    fake.capture_gate = asyncio.Event()
    async with Client(create_server(service), mode=mode) as client:
        started = await client.call_tool("reactor_start", {"prompt": "forest"})
        handle = started.structured_content["handle"]
        await service.sessions[handle].task
        capture = await client.call_tool("reactor_capture", {"handle": handle})
        capture_handle = capture.structured_content["capture_handle"]
    assert service.status(handle)["phase"] == "closed"
    assert service.capture_status(capture_handle)["phase"] == "cancelled"
    assert fake.disconnected == fake.closed == 1
