# Reactor MCP

An unofficial MCP server for running [Reactor](https://reactor.inc) video models from Claude Code or Codex. It supports two models:

- **FastH3-Omni**: queued 6–14 second clips with synchronized audio. Clips can use reference images and audio, pin a first or last frame, and continue earlier clips.
- **Helios**: a continuous real-time stream you steer with text prompts.

Recordings are downloaded to local files. Setup also connects Reactor's official docs MCP and can install a workflow skill.

**Status:** Omni has been tested in short live sessions (text clips, reference images, continuation, recording). Helios has only been tested against mocks. See [VALIDATION.md](VALIDATION.md).

## Setup (macOS)

Install [uv](https://docs.astral.sh/uv/) and the Claude Code or Codex CLI, then run:

```sh
uvx --python 3.12 --from git+https://github.com/alexmeckes/reactor-mcp@v0.4.1 reactor-mcp setup
```

The wizard:

- asks which client to connect (Claude Code, Codex, or both);
- stores your API key in macOS Keychain, through a hidden prompt;
- registers two user-scoped connections, `reactor_inference` and `reactor_docs`;
- asks whether to allow paid sessions and whether to install the workflow skill.

It doesn't start any sessions. Restart your client afterwards.

`reactor-mcp doctor` checks the local configuration without contacting Reactor or reading Keychain.

To update to a new release, remove the old entry (`claude mcp remove --scope user reactor_inference`) and rerun setup. Setup won't overwrite a skill that differs from its own copy, so replace `~/.claude/skills/reactor-workflow/SKILL.md` yourself.

Settings live in `~/.config/reactor-mcp/settings.json`, and recordings go to `~/Movies/Reactor`. The environment variables `REACTOR_API_KEY`, `REACTOR_ENABLE_LIVE`, and `REACTOR_OUTPUT_DIR` override the saved settings.

## Tools

| Tool | What it does |
| --- | --- |
| `reactor_describe` | Show supported models, configuration, and limits (no network) |
| `reactor_omni_start` | Start an Omni session: aspect ratio, autoplay, and black frames or a held last frame between clips |
| `reactor_omni_enqueue` | Upload references and queue a clip; returns its `clip_id` |
| `reactor_omni_control` | `play`, `stop`, `pop`, `move`, or `reset` clips |
| `reactor_start` | Start a Helios stream with a prompt |
| `reactor_set_prompt` / `reactor_control` | Steer a Helios stream, or pause and resume it |
| `reactor_status` | Connection, model state, per-clip phases, errors, and time left |
| `reactor_capture` / `reactor_capture_status` | Record the last N seconds or the whole session, and download it |
| `reactor_close` | End the session |

Starting a session is billable, and billing continues while paused. Only one session runs at a time, capped at 15–300 seconds.

## Using Omni

1. **Start:** `reactor_omni_start`. The aspect ratio (16:9, 1:1, 9:16, or 4:3) is fixed for the session.
2. **Queue:** `reactor_omni_enqueue` with a prompt. Describe the subject, one action, the camera, the dialogue (or "None"), the sound, and the music (or "None"); see the [prompt guide](https://docs.reactor.inc/model-api-reference/fast-h3-omni/prompt-guide).
   - Images and audio are absolute local paths that get uploaded to Reactor. Refer to them in the prompt as `<Picture N>` and `<Audio N>`.
   - Use 3 or fewer images to keep builds real-time.
3. **Continue:** set `continue_from_clip_id` to chain shots. Only the session's 8 most recent clips can be continued. Adding new reference images to a continuation brings those objects into the ongoing scene.
4. **Watch:** poll `reactor_status`. Its `clips` list shows each clip's phase and build time. In testing, clips built in 3–7 seconds.
5. **Record:** after the last clip you want has played, start `reactor_capture` with `full_session=true` while another clip is still playing. A recording only finishes while output keeps streaming, so keep queuing short throwaway clips until it completes. Then close the session.

When the model refuses a request, the tool returns `COMMAND_REFUSED: <code>`, for example `queue_full` or `request_rejected` for a prompt that's too long. A refused request changes nothing.

## Using Helios

Call `reactor_start` with a prompt, then poll `reactor_status` until it's ready. Steer with `reactor_set_prompt`, record with `reactor_capture`, and finish with `reactor_close`.

## Limits

- **Session lifetime:** handles only exist inside the server process. Closing the process ends sessions; if that fails, Reactor's server-side duration cap is the fallback.
- **Uploads:** only image files (JPEG, PNG, WebP) and audio files (WAV, MP3, AAC/M4A, OGG/Opus, FLAC, WebM) up to 25 MiB.
- **Downloads:** up to 256 MiB. Recordings are saved as `.mp4`, or `.ts` for older stream formats.
- **Credentials:** the API key is exchanged for a short-lived token limited to one session of the chosen model. Keys and tokens are never returned in tool results.

## Development

```sh
git clone https://github.com/alexmeckes/reactor-mcp.git && cd reactor-mcp
uv sync --locked --python 3.12
uv run --locked pytest -q
uv run --locked ruff check src tests examples
```

The server speaks stdio MCP. For a manual client config, see `examples/mcp-client.json` and `.env.example`; the server doesn't load `.env` files itself.
