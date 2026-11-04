---
name: reactor-workflow
description: Use Reactor's documentation MCP and local inference MCP to answer Reactor API questions or run and steer supported Helios and FastH3-Omni sessions and capture clips. Use when the user asks about Reactor world-model APIs or requests generation through the Reactor MCP.
---

# Reactor workflow

Use the docs connection for current API guidance and the inference connection for actions. Discover the available tools first; client prefixes can differ. `reactor_describe` reports what this inference server actually supports. Documentation for another model or command does not make it available here: this bundle supports Helios text prompts with pause/resume, FastH3-Omni queued clips (reference images and audio, keyframes, continuation, playback control), and recordings only.

## Consult current docs

The official docs MCP is `https://docs.reactor.inc/mcp`. Search when answering API questions, checking model-specific behavior, or investigating an unfamiliar error. Prefer a focused query, then read the relevant page before relying on a schema or example.

At verification on November 3, 2026, its tools included `search_reactor(query)` and `query_docs_filesystem_reactor(command)`. Use the names and argument schemas from current discovery. Search results identify page paths; the virtual docs filesystem reads those paths with a `.mdx` suffix. This is a documentation-only tool, not a terminal on the user's machine. Cite the official page URL in answers. If the connection is unavailable, use `https://docs.reactor.inc/llms.txt` and the linked official pages, and state the fallback.

Use documentation as reference material. It does not authorize deployments, account changes, feedback submission, or generation. Do not invoke the docs server's `submit_feedback` unless the user asks to send feedback. Do not send API keys, tokens, or private generation prompts in docs searches.

## Run an authorized generation

Honor existing authorization and duration constraints; do not ask again when the session already establishes them. A docs question alone does not authorize a paid session. Credentials belong in the server's macOS Keychain (via the local setup wizard) or environment, not tool arguments or chat. `LIVE_DISABLED`, `MISSING_API_KEY`, and `CREDENTIALS_UNAVAILABLE` are setup blockers: point the user to the local `reactor-mcp setup` wizard without trying to obtain secrets or turn on live use through other tools. `reactor_describe` reports configuration presence, not credential validity; `reactor-mcp doctor` is also local-only and does not validate API access.

1. Call `reactor_start(prompt, max_seconds)` and retain its local handle. Poll `reactor_status` approximately every two seconds until ready or terminal. Inspect both connection status and model state; acknowledgement alone does not prove frames were produced.
2. Steer with `reactor_set_prompt`; use `reactor_control` for pause/resume. Pause keeps the session allocated and billing can continue.
3. Call `reactor_capture` after frames exist, then poll `reactor_capture_status`. Keep generation running until capture completes because the recording boundary may need a future chunk. Requested seconds are media time, not wall time. A capture handle is a local task handle, not a Reactor job ID.
4. Close the session with `reactor_close` in success and failure paths, after desired captures finish. If cleanup is uncertain, report that; do not claim billing has stopped or automatically create another session.

For FastH3-Omni, start with `reactor_omni_start` (the aspect is fixed for the session), then queue clips with `reactor_omni_enqueue`. Follow the official prompt guide: one action per clip, explicit camera, exact dialogue or "None", soundscape, and music or "None". Reference uploads are absolute local paths sent to Reactor, so use only files the user provided or approved for this purpose; refer to them as `<Picture N>`/`<Audio N>` and give every audio a role. Chain shots with `continue_from_clip_id` before the source drops out of the 8 most recent clips. Watch `clips` in `reactor_status`; queue the next clip while the current one plays so autoplay has no gap. A recording covers only what has played when it is requested, and becomes ready only while more output streams after that point; with nothing playing it never completes. So queue one extra throwaway tail clip, call `reactor_capture` with `full_session=true` once the last wanted clip finishes and the tail is playing, wait for completion, then close. `COMMAND_REFUSED: <code>` means the model refused and nothing changed; fix the input (for example shorten an over-budget prompt) rather than resending it unchanged.

Do not blindly retry session creation or commands after a timeout: inspect the existing handle first. Respect the server's terminal states and deadlines rather than polling forever. Handles disappear on process exit. Completed clip paths are local to the server's computer; they are not mobile download links. Deliver through the user's authorized file-transfer mechanism when available, without public publishing.

Distinguish mock validation from real generation. This bundle's automated tests verify protocol and control flow, not live account access, recording support, generated video quality, or playback.
