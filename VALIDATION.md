# Local validation

Date: November 3, 2026. Release: 0.2.0. Environment: macOS arm64, Python 3.12.13, MCP SDK 2.3.0, Reactor SDK 1.13.0.

## Passed

- 42 automated tests (`.venv/bin/pytest -q`).
- Ruff lint and format checks on source, tests, and example client.
- Real MCP stdio subprocess tests of modern `2026-07-28` discovery and legacy `2025-11-25` initialization, eight-tool discovery with input/output schemas, configuration inspection, and rejection of generation while live use is disabled.
- MCP client/server in-memory tests exercise the mocked start → status → prompt → capture → result → close workflow on both protocol paths. Annotations are checked after serialization; client exit closes active sessions and cancels unfinished captures.
- The companion `skills/reactor-workflow/SKILL.md` passes the skill-creator validator. Its guidance was reviewed against docs-only, authorized-generation, missing-credentials, unsupported-model, and uncertain-cleanup scenarios. This is an instruction review, not a live agent behavior evaluation.
- The two-server example JSON parses successfully. Neither server configuration nor the skill was installed into an existing client.
- Official Reactor SDK native library loads on this machine; expected Python methods exist.
- Mocked HTTP token exchange verifies the exact `/tokens` URL, API-key header, Helios model scope, one-session constraint, and duration constraint.
- Mocked HTTP 202 readiness, MP4 init/fragment ordering, legacy TS output, cross-origin credential isolation, HTTP error redaction, rejected redirects, size limits, and cancellable readiness waits.
- Session limits, failed initialization cleanup, remote command rejection, rate limiting without retry, automatic expiry, concurrent close, cancellation-safe cleanup, and cancelled captures.

The test suite prohibits real HTTP through HTTPX and HTTPX2 default async transports. Tests inject SDK fakes for sessions and mock HTTP responses for token exchange/downloads. The fake media fixture is **not a generated video**.

## Public docs verification

Separately from the offline suite, an unauthenticated `mcp.Client` connection to `https://docs.reactor.inc/mcp` negotiated `2026-07-28`. Tool discovery returned `search_reactor`, `query_docs_filesystem_reactor`, and `submit_feedback`. One read-only `search_reactor` call for “Helios set_prompt pause recording request_clip” succeeded and returned official Helios/recording documentation links. Feedback was not sent. This verifies public documentation lookup, not inference.

## Not yet verified

No live authenticated API or billable generation call was made. Actual account/model access, token exchange acceptance, GPU allocation, WebRTC negotiation, model events, server-side cap enforcement, recording availability, and video decode/playback require an approved live trial and credentials supplied securely outside chat.

At this validation checkpoint, no credentials were retrieved or configured, no client configuration was changed, and nothing had been published, pushed, or deployed. Subsequent publication of the source repository does not constitute live inference validation. The source archive and package build contain only project files, not `.venv`, caches, credentials, or generated media.

MCP Tasks durability, portable clip resource delivery, client-specific GUI installation, and hosted HTTP operation remain outside this revision. Legacy compatibility was tested using SDK v2's legacy protocol mode, not every older client product.
