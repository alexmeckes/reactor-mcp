# Reactor inference MCP

A local, unofficial MCP server for **Reactor Helios inference**, built with the official Python MCP SDK and `reactor-sdk`. Version 0.2.0 includes a configuration for Reactor's official docs MCP and a companion workflow skill. It opens live sessions, changes prompts, pauses/resumes generation, captures clips to local files, and terminates sessions.

This is an implemented, mock-tested MVP. **Authenticated generation and real video playback have not been validated.** No keys have been configured and no paid session has been started.

## Install and run

Requires Python 3.11+ and `uv`. Tested on macOS arm64 with Python 3.12.13, MCP SDK **2.3.0**, Reactor SDK 1.13.0. Reactor's native SDK wheel must support your OS/architecture. The server supports the current `2026-07-28` protocol and legacy `2025-11-25` clients; both paths are exercised in protocol tests.

```sh
git clone https://github.com/alexmeckes/reactor-mcp.git
cd reactor-mcp
uv sync --locked --python 3.12
uv run --locked reactor-mcp
```

If you downloaded the source ZIP instead, extract it and enter its `reactor-mcp` directory before running `uv sync`.

The server speaks **stdio MCP**; running it directly waits for protocol input. There is no HTTP port or hosted service. A safe protocol inspection script is included:

```sh
uv run --locked python examples/inspect_mcp.py
uv run --locked pytest -q
uv run --locked ruff check src tests examples
uv run --locked ruff format --check src tests examples
```

In a restricted workspace, set `UV_CACHE_DIR` to an allowed cache directory before invoking `uv`. An existing `.venv` can run the tests directly without package downloads.

## MCP client configuration

Adapt `examples/mcp-client.json` to your client. It uses Cursor-style `mcpServers` configuration with local stdio and remote HTTP entries; other clients may require different configuration syntax or separate UI setup. Replace both absolute paths. This example has not been installed into any client.

```json
{
  "mcpServers": {
    "reactor_inference": {
      "command": "/absolute/path/to/reactor-mcp/.venv/bin/python",
      "args": ["-m", "reactor_mcp.server"],
      "env": {
        "REACTOR_ENABLE_LIVE": "false",
        "REACTOR_OUTPUT_DIR": "/absolute/path/to/reactor-mcp/outputs"
      }
    },
    "reactor_docs": {
      "url": "https://docs.reactor.inc/mcp"
    }
  }
}
```

Discovery works without an API key. For a later approved live trial, supply `REACTOR_API_KEY` to the server through your client's secure environment/secret mechanism, then explicitly set `REACTOR_ENABLE_LIVE=true`. Do not paste a key into chat or commit it. A GUI client may not inherit your terminal environment. `.env.example` documents variables; the server does **not** automatically load `.env` files.

The API key is exchanged at `POST https://api.reactor.inc/tokens` for a token restricted to `reactor/helios`, one session, and the requested duration cap. The SDK receives only that token. Keys, tokens, authenticated playlist URLs, and raw upstream error bodies are not returned in tool results.

## Docs search and companion skill

The two connections have distinct roles: `reactor_inference` exposes the eight actions below, while `reactor_docs` searches Reactor's maintained documentation. No duplicate search index or proxy is needed. On November 3, 2026, a real, unauthenticated MCP connection negotiated `2026-07-28`, discovered `search_reactor` and `query_docs_filesystem_reactor`, and successfully searched for Helios prompt, pause, and recording behavior. Tool names should still come from discovery because the upstream service can change them. Its additional `submit_feedback` tool sends feedback to Reactor; it is not required for lookup and should only be used when requested.

The source ZIP includes [skills/reactor-workflow/SKILL.md](skills/reactor-workflow/SKILL.md). To use it in a skill-capable client, copy the `reactor-workflow` folder into that client's skill directory, or load the file as workflow instructions. Installing the skill does not configure either MCP connection or enable paid usage. No skill was installed into your existing environment by this change.

The skill guides docs lookup, the supported Helios workflow, failure recovery, billing awareness, and clip delivery. It preserves the boundary between a documentation query and authorization to run generation. For example: “Use Reactor's docs to explain how pause affects billing” should only search/read documentation; an already-authorized clip request should use the inference tools and close the session afterward.

## Tools

Every tool exposes JSON input and output schemas plus MCP behavior annotations.

| Tool | Purpose |
| --- | --- |
| `reactor_describe` | Supported model, configuration presence, workflow, and limits; no network |
| `reactor_start(prompt, max_seconds=60)` | Begin connection and generation in a background task; returns immediately with a local session handle |
| `reactor_status(handle)` | Connection state, latest model state, errors, upstream session ID, and remaining local lifetime |
| `reactor_set_prompt(handle, prompt)` | Send Helios `set_prompt` |
| `reactor_control(handle, action)` | Send `pause` or `resume` |
| `reactor_capture(handle, seconds=5)` | Capture the last N media seconds and download asynchronously |
| `reactor_capture_status(capture_handle)` | Capture phase, error, or completed file path and clip metadata |
| `reactor_close(handle)` | End the session, cancel unfinished capture, and release local resources |

## End-to-end workflow

After live usage is explicitly approved and credentials are securely available:

1. Call `reactor_describe` to check configuration.
2. Call `reactor_start` with `{"prompt":"A slow camera move through a sunlit forest","max_seconds":120}`. This can incur charges.
3. Poll `reactor_status` with the returned **handle**, about every 2 seconds, until `phase` and `connection_status` are `ready`. Stop on `failed` or `closed`. Inspect `error`; `model_state.running` and `current_frame` provide generation evidence when model events arrive.
4. Optionally call `reactor_set_prompt` to steer the scene. `pause` leaves the GPU session allocated and does not stop billing.
5. After some frames have been generated, call `reactor_capture` with `{"handle":"<returned handle>","seconds":5}`. Poll `reactor_capture_status` with its `capture_handle` until `complete`, `failed`, or `cancelled`.
6. Keep generation running until capture is complete: Reactor may wait for the recording chunk containing the clip boundary to finish. The completed `path` points to a file on the **MCP server's computer**, which can be opened in a compatible media player.
7. Call `reactor_close` in all success and failure paths. `cleanup=disconnect_acknowledged` means the SDK disconnect completed; `cleanup=uncertain` means termination was not confirmed and the server duration cap remains the fallback.

These are live WebRTC sessions, **not a REST job queue**. The session/capture handles are generated by this MCP process; do not use them as upstream Reactor job IDs. Session connection phases are separate from Helios generation state. Command acknowledgement can precede model events, and an asynchronous `command_error` remains visible in status.

## Scope and operational limits

- Helios only, using documented `set_prompt`, `start`, `pause`, and `resume` commands. No arbitrary-command escape hatch, uploads, live video player, audio, session adoption, deployment, or other model schemas.
- One session and one pending capture per process. Sessions default to 60 seconds and accept 15–300 seconds. The local timer includes connection time; the token independently caps the server session's duration.
- Connection deadline 45 seconds, command deadline 15 seconds, capture deadline 120 seconds, HTTP request timeout 15 seconds. No automatic retries of session creation or commands. Clip readiness polls only repeat the download GET after HTTP 202.
- Graceful stdio shutdown closes sessions. Hard process termination or network loss cannot confirm termination; the server-side cap is the fallback. This is not a dollar-denominated spending cap, and separate MCP processes have independent session limits.
- Clips require recording enabled on the model. A requested clip length is media time, not time spent waiting. Closing/pausing too early or the session cap expiring can prevent capture completion. Model availability, recording support, permissions, and playback remain live-test questions.
- Clip downloads are limited to 256 MiB, written to generated filenames in `REACTOR_OUTPUT_DIR` (default: `outputs` relative to the process working directory). Partial files are removed on failure/cancellation. Completed files persist; handles and the last 20 session/capture records do not survive process exit. Remove unwanted clips yourself.
- The downloader supports simple, unencrypted HLS media playlists. It preserves the fragmented-MP4 init segment and writes `.mp4`; legacy TS playlists produce `.ts`. Master, encrypted, byte-range, discontinuous, and redirected playlists are explicitly rejected. Bearer tokens are sent only to `https://api.reactor.inc` on its standard port, never to external storage/CDN segment URLs.
- HTTP environment proxies are disabled for authentication/downloads (`trust_env=False`). A network requiring a proxy may need an explicit adapter change. The SDK's WebRTC transport has its own networking requirements.

## Documentation and implementation evidence

Reviewed November 3, 2026:

- [Reactor Python SDK reference](https://docs.reactor.inc/sdk-reference/python/reactor): connection lifecycle, correlated commands, clip requests, and disconnect semantics.
- [Helios command/event schema](https://docs.reactor.inc/model-api-reference/helios/schema): exact model commands and authoritative `state` event fields.
- [Authentication](https://docs.reactor.inc/authentication): API-key header, `/tokens` exchange, session scoping, and duration constraints.
- [Recordings](https://docs.reactor.inc/concepts/recordings): clip readiness, recording availability, and retention.
- [Billing](https://docs.reactor.inc/resources/billing): billing follows session allocation, including pauses.
- [Official Reactor SDK source](https://github.com/reactor-team/reactor-client-sdks): installed 1.13.0 `client.py`, `_auth.py`, and `_recording.py` were inspected against the docs. The recordings guide describes Python output as MPEG-TS, while this SDK supports fragmented MP4. The downloader detects the actual manifest format instead of assuming one.
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) and [v2 migration guide](https://py.sdk.modelcontextprotocol.io/migration/): `MCPServer`, `Client`, snake-case annotation fields, explicit `ToolError` failures, typed results, stdio, and lifespan cleanup.

`src/reactor_mcp/backend.py` uses bounded asynchronous HTTP for token exchange and download. Session control stays in the official Reactor SDK. This avoids the installed SDK downloader's blocking HTTP worker, whose requests have no explicit socket timeout. No undocumented inference REST endpoint was invented.

See `VALIDATION.md` for local test evidence and remaining verification limits.

This revision keeps the explicit local session/capture handles and local clip paths. Standard MCP Tasks and portable clip resource delivery remain future work; the server does not advertise durable Tasks or imply that a local file path is remotely downloadable.
