"""stdio MCP entry point; stdout is reserved for the MCP protocol."""

from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from .models import CaptureStatus, CommandResult, SessionStatus
from .service import Service

Prompt = Annotated[str, Field(min_length=1, max_length=8000, pattern=r"\S")]
Handle = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


def create_server(service: Service | None = None) -> MCPServer:
    service = service or Service()

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            await service.shutdown()

    server = MCPServer(
        "Reactor inference",
        version="0.2.0",
        lifespan=lifespan,
        instructions="Helios streaming inference. Start is billable. Poll status; capture while running; "
        "wait for capture completion before closing. Local handles are not upstream job IDs.",
    )
    read = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    write = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    )

    @server.tool(annotations=read)
    def reactor_describe() -> dict[str, Any]:
        """Describe this server's supported model, workflow, configuration and limits; no network use."""
        return service.describe()

    @server.tool(annotations=write)
    async def reactor_start(
        prompt: Prompt,
        max_seconds: Annotated[int, Field(ge=15, le=300)] = 60,
    ) -> SessionStatus:
        """Start a BILLABLE Helios session asynchronously. Poll reactor_status using the returned handle.

        Requires REACTOR_ENABLE_LIVE=true and a server-side API key. One live session at a time.
        The duration cap includes local connection time. Never automatically retry a failed start.
        """
        return service.start(prompt, max_seconds)

    @server.tool(annotations=read)
    def reactor_status(handle: Handle) -> SessionStatus:
        """Read connection state, model events, errors and remaining lifetime for a local session handle."""
        return service.status(handle)

    @server.tool(annotations=write)
    async def reactor_set_prompt(handle: Handle, prompt: Prompt) -> CommandResult:
        """Steer Helios with new text. Applies at the next chunk while running, current chunk if paused."""
        return await service.command(handle, "set_prompt", {"prompt": prompt})

    @server.tool(annotations=write)
    async def reactor_control(handle: Handle, action: Literal["pause", "resume"]) -> CommandResult:
        """Pause or resume generation. Pausing DOES NOT stop billing; use reactor_close to end it."""
        return await service.command(handle, action, {})

    @server.tool(annotations=write)
    async def reactor_capture(
        handle: Handle,
        seconds: Annotated[float, Field(gt=0, le=60)] = 5,
    ) -> CaptureStatus:
        """Capture the last N media seconds and download in a background local task (120s deadline).

        Poll reactor_capture_status. Keep generation running until complete; recording must be enabled
        on the model. Returns a local capture handle, not an upstream Reactor job ID.
        """
        return service.capture(handle, seconds)

    @server.tool(annotations=read)
    def reactor_capture_status(capture_handle: Handle) -> CaptureStatus:
        """Read capture progress or the completed local .mp4/.ts file path. File is on the server's host."""
        return service.capture_status(capture_handle)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        )
    )
    async def reactor_close(handle: Handle) -> SessionStatus:
        """End the session and release its GPU; cancels incomplete captures. Safe to call repeatedly."""
        return await service.close(handle)

    return server


def main():
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
