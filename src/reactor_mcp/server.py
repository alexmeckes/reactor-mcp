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
ClipId = Annotated[str, Field(pattern=r"^[0-9A-Za-z-]{1,64}$")]
LocalPath = Annotated[str, Field(min_length=1, max_length=4096)]
Duration = Annotated[int, Field(ge=15, le=300)]


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
        version="0.4.0",
        lifespan=lifespan,
        instructions="Helios streaming and FastH3-Omni clip inference. Starts are billable. Poll status; "
        "capture while running; wait for capture completion before closing. Local handles are not "
        "upstream job IDs.",
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
    async def reactor_start(prompt: Prompt, max_seconds: Duration = 60) -> SessionStatus:
        """Start a BILLABLE Helios session asynchronously. Poll reactor_status using the returned handle.

        Requires live use enabled via setup or environment and a server-side key. One session at a time.
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
    async def reactor_omni_start(
        max_seconds: Duration = 120,
        aspect: Literal["16:9", "1:1", "9:16", "4:3"] = "16:9",
        autoplay: bool = True,
        flush_on_clip_end: bool = True,
    ) -> SessionStatus:
        """Start a BILLABLE FastH3-Omni session (video clips with synchronized audio). Poll reactor_status.

        Same live/key/one-session rules as reactor_start. The aspect is fixed for the session.
        Autoplay plays ready clips in queue order; flush_on_clip_end=False holds the last frame
        between clips instead of cutting to black. Never automatically retry a failed start.
        """
        return service.start_omni(max_seconds, aspect, autoplay, flush_on_clip_end)

    @server.tool(annotations=write)
    async def reactor_omni_enqueue(
        handle: Handle,
        prompt: Prompt,
        seconds: Literal[5.875, 8.0, 10.125, 12.25, 14.375] | None = None,
        reference_images: Annotated[list[LocalPath], Field(max_length=9)] = [],
        reference_audios: Annotated[list[LocalPath], Field(max_length=3)] = [],
        first_frame_image: LocalPath | None = None,
        last_frame_image: LocalPath | None = None,
        continue_from_clip_id: ClipId | None = None,
        continue_mode: Literal["latent", "frame"] | None = None,
        seed: Annotated[int, Field(ge=0)] | None = None,
        position: Annotated[int, Field(ge=0)] | None = None,
        metadata: Annotated[str, Field(max_length=2000)] = "",
    ) -> CommandResult:
        """Queue one Omni clip; returns its clip_id. Clips build while others play.

        Write the prompt as subject/setting, one action, camera, dialogue (exact words or "None"),
        soundscape, music (or "None"). Images and audio are absolute local paths that are uploaded
        to Reactor: refer to them as <Picture N> / <Audio N> and give each audio a role (voice,
        sound, music). Use ≤3 images for real-time builds. Keyframes need a reference image.
        continue_from_clip_id must be one of the session's 8 most recent clips; latent mode carries
        motion, camera and sound, frame mode starts from its last frame. Seconds default to 14.375.
        """
        return await service.enqueue(
            handle,
            prompt,
            reference_images=reference_images,
            reference_audios=reference_audios,
            first_frame_image=first_frame_image,
            last_frame_image=last_frame_image,
            continue_from_clip_id=continue_from_clip_id,
            continue_mode=continue_mode,
            seconds=seconds,
            seed=seed,
            position=position,
            metadata=metadata,
        )

    @server.tool(annotations=write)
    async def reactor_omni_control(
        handle: Handle,
        action: Literal["play", "stop", "pop", "move", "reset"],
        clip_id: ClipId | None = None,
        position: Annotated[int, Field(ge=0)] | None = None,
    ) -> CommandResult:
        """Control Omni playback and queues: play (clip_id, or the front ready clip), stop, pop a
        clip, move a clip to a queue position (0 = front), or reset the session's queues.

        A played, stopped or popped clip cannot be replayed. Clip phases appear in reactor_status.
        """
        return await service.omni_control(handle, action, clip_id, position)

    @server.tool(annotations=write)
    async def reactor_capture(
        handle: Handle,
        seconds: Annotated[float, Field(gt=0, le=60)] = 5,
        full_session: bool = False,
    ) -> CaptureStatus:
        """Capture the last N media seconds, or the whole session, and download it in a background
        local task (120s deadline, 300s for a full session).

        Poll reactor_capture_status. Keep generation running until complete; recording must be enabled
        on the model. Omni clips are only in the stream once played. Returns a local capture handle,
        not an upstream Reactor job ID.
        """
        return service.capture(handle, None if full_session else seconds)

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
