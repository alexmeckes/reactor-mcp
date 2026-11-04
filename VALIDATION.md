# Local validation

## 0.4.1 (live FastH3-Omni)

Date: November 4, 2026. Two capped live sessions ran through the released v0.4.0 server. Token exchange for `reactor/fast-h3-omni`, WebRTC connection (about 6 seconds), `enqueue`, autoplay, and clip phase tracking worked; 5.875-second text clips built in 3.3–3.7 seconds. A full-session recording requested with nothing left to play returned HTTP 202 until the 150-second cap cancelled it. A recording requested while the next clip played completed in about 4 seconds: a 1344×768 H.264 + AAC fragmented MP4 covering the clips played so far. A third live session uploaded a scene image as reference and first-frame keyframe, then queued a latent continuation with two object reference images; both objects appeared in the continued scene and transformed as prompted. Its full-session capture needed two tail clips and about 25 seconds to complete. Audio references, last-frame keyframes, `frame` continuation, and the `move`/`pop`/`stop`/`reset` controls were not exercised live.

## 0.4.0 (FastH3-Omni)

Date: November 4, 2026. 73 automated tests and Ruff lint/format checks pass. New tests cover Omni token scope and SDK construction, start-time canvas/autoplay/flush commands, reference image/audio/keyframe uploads mapped into `enqueue`, clip phase tracking from replies and broadcasts (including a stale reply arriving after generation), sanitized failure reasons, model refusal codes, wrong-model guards, rejected non-media and symlinked upload paths, full-session capture, and MCP schema validation of Omni arguments. The real SDK exposes `upload_file` and `request_recording`.

Not re-run for 0.4.0: the wheel/sdist build check and real client registration below. No live Omni session was started, so token acceptance for `reactor/fast-h3-omni`, upload acceptance, clip quality, and whether recording is enabled for this model are unverified.

## 0.3.0

Date: November 3, 2026. Release: 0.3.0. Environment: macOS arm64, Python 3.12.13, MCP SDK 2.3.0, Reactor SDK 1.13.0, keyring 25.7.0.

## Passed

- 68 automated tests (`.venv/bin/pytest -q`).
- Ruff lint and format checks on source, tests, and example client.
- Real MCP stdio subprocess tests of modern `2026-07-28` discovery and legacy `2025-11-25` initialization, eight-tool discovery with input/output schemas, configuration inspection, and rejection of generation while live use is disabled.
- MCP client/server in-memory tests exercise the mocked start → status → prompt → capture → result → close workflow on both protocol paths. Annotations are checked after serialization; client exit closes active sessions and cancels unfinished captures.
- The companion `skills/reactor-workflow/SKILL.md` passes the skill-creator validator. Its guidance was reviewed against docs-only, authorized-generation, missing-credentials, unsupported-model, and uncertain-cleanup scenarios. This is an instruction review, not a live agent behavior evaluation.
- The two-server example JSON parses successfully. Neither server configuration nor the skill was installed into an existing client.
- Official Reactor SDK native library loads on this machine; expected Python methods exist.
- Mocked HTTP token exchange verifies the exact `/tokens` URL, API-key header, Helios model scope, one-session constraint, and duration constraint.
- Mocked HTTP 202 readiness, MP4 init/fragment ordering, legacy TS output, cross-origin credential isolation, HTTP error redaction, rejected redirects, size limits, and cancellable readiness waits.
- Session limits, failed initialization cleanup, remote command rejection, rate limiting without retry, automatic expiry, concurrent close, cancellation-safe cleanup, and cancelled captures.
- Setup tests cover both client command shapes, repeated setup, preserving conflicting entries/custom skills, partial registration failure, private non-secret settings, environment precedence, lazy fake Keychain retrieval, redacted errors, hidden-prompt prerequisites, and saved preferences driving mocked inference. Tests use temporary home/config directories and prohibit real Keychain access.
- Real installed `claude mcp add` and `codex mcp add` commands registered both connections in isolated temporary configurations. Readback and rerun checks passed; the optional skill was installed only under the temporary home. No existing user client configuration was changed or MCP health check triggered.
- Built a source distribution and wheel, installed the wheel with dependencies into a fresh temporary environment, and verified bundled skill data, the console `doctor` command, and a real stdio handshake with eight tools and live use disabled. No API request was made by these checks. An initial offline installation lacked cached dependencies; the fresh installation succeeded with package downloads enabled.
- The uv lockfile is current and the wheel includes the canonical workflow skill. Setup launches a versioned Git source through `uvx`; no cached virtualenv executable path is embedded in client entries.

The test suite prohibits real HTTP through HTTPX and HTTPX2 default async transports. Tests inject SDK fakes for sessions and mock HTTP responses for token exchange/downloads. The fake media fixture is **not a generated video**.

## Public docs verification

Separately from the offline suite, an unauthenticated `mcp.Client` connection to `https://docs.reactor.inc/mcp` negotiated `2026-07-28`. Tool discovery returned `search_reactor`, `query_docs_filesystem_reactor`, and `submit_feedback`. One read-only `search_reactor` call for “Helios set_prompt pause recording request_clip” succeeded and returned official Helios/recording documentation links. Feedback was not sent. This verifies public documentation lookup, not inference.

## Not yet verified

No live authenticated API or billable generation call was made. Actual account/model access, token exchange acceptance, GPU allocation, WebRTC negotiation, model events, server-side cap enforcement, recording availability, and video decode/playback require an approved live trial and credentials supplied securely outside chat.

No real credentials were retrieved or configured, and no existing user client configuration was changed. The source repository is public at `https://github.com/alexmeckes/reactor-mcp`; publication does not constitute live inference validation. Package builds contain project files, not `.venv`, caches, credentials, or generated media.

Real Keychain storage/retrieval, the macOS permission dialog, client UI connection behavior, and authenticated inference still require a user-run setup/live trial. The CLI registration paths were tested; no Claude or Codex conversation was used to generate media. MCP Tasks durability, portable clip resource delivery, client-specific GUI installation, and hosted HTTP operation remain outside this revision. Legacy compatibility was tested using SDK v2's legacy protocol mode, not every older client product.
