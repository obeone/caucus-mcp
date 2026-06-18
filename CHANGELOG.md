# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The single source of truth for the version is `[project].version` in
`pyproject.toml`.

## [Unreleased]

## [1.2.1] — 2026-06-18

### Security

- **Dependencies** — refreshed the lockfile to pull patched versions
  addressing upstream security advisories.
- **CI** — restricted the workflow `GITHUB_TOKEN` to read-only
  (`contents: read`).

## [1.2.0] — 2026-06-18

Second hardening pass, focused on the configurable hub URL and resilience.

### Security

- **URL guard** — fail-closed validation for the operator-configurable hub
  URL, shared across every connector.
- **Bridge / watcher / agent** — guard the hub URL, harden the watcher token
  file, tolerate malformed hub responses, and survive transient hub blips with
  bounded retry/backoff.
- **Hub** — limit request body size, gate `/export`, and add a console CSP.
- **Disk log** — write the pruned event log atomically and serialize it with
  appends to avoid corruption.
- Regression tests covering the Low-severity hardening items.

## [1.1.0] — 2026-06-18

First security hardening pass after the stable release.

### Security

- **Prompt-injection containment** — inbound peer messages are treated as
  untrusted by the native agent.
- **Identity & provenance** — reserve the operator and hub identities and stamp
  every message with its origin.
- **Resource caps** — cap in-memory resources, gate the UI origin (anti-CSWSH),
  authenticate the `/control` channel, and enforce throughput caps.
- **Rate limit** — read-only `available()` probe on the token bucket.
- Test suite covering auth, CSWSH, caps, throttle, and provenance.

## [1.0.0] — 2026-06-17

First stable release. The protocol, HTTP API, and CLI surface are now
considered stable under SemVer.

### Highlights

- **Supervised multi-agent hub** — a FastAPI process where agents talk
  directly, by broadcast, or in private `#`-channels, all under a human
  operator who watches live and can pause, stop, reset, or kick.
- **Two connectors over one hub** — a passive `caucus-bridge` (with the
  zero-token `caucus-watch` listener) for turn-based MCP hosts, and a native
  autonomous `caucus-claude-agent` on the Claude Agent SDK that owns its loop.
- **Hub-owned operating protocol** — served versioned at `/protocol`; clients
  fetch it at `setup()` and re-read it when `PROTOCOL_VERSION` moves.
- **Talking stick** floor control — any peer can seize a lane so a grave
  message is heard; the operator can clear it.
- **Private channels** with topics and a connect-time directory; convener role
  for coordinated closes.
- **Operator forms** — an agent pushes a questionnaire, the operator answers
  once in a console wizard, and the bundle routes back as an `answer` message.
- **Agent profiles** — `talker` (caucus tools only) vs `worker` (also wields
  built-in Claude Code tools), with a selectable permission mode.
- **Operator dashboard SPA** (Vite + React + TS + Tailwind + shadcn) served by
  the hub, with Health / Flow / Channels / Forms panels over the `/ui`
  WebSocket; optional operator/observer token auth and RBAC.
- **Loop safety** — per-sender token-bucket rate limiting and a hard operator
  Stop every agent observes; an idle reaper drops quiet peers.
- **Observability** — message sequence numbers with ACK and replay on
  reconnect, an opt-in append-only JSONL event log, and a `/export` endpoint.

## Pre-1.0 history

The 0.1 → 0.20 series built the project up in these milestones (see the git
history for per-commit detail):

- **0.1–0.3 — Foundations.** War-room hub + MCP bridge package, operator
  console served by the hub, passive-until-`join` bridge, and a versioned
  operating protocol with a `setup()` gate and version handshake.
- **0.4–0.6 — Listening model.** Zero-token background `caucus-watch` listener
  made the default, idle-peer reaping with `POST /leave`, and the one-shot
  watcher-relaunch contract.
- **0.7–0.9 — Native path & channels.** Async `HubConnector` and the
  autonomous Claude connector on the Agent SDK; private channels with routing,
  per-channel topics, and a connect-time directory; Markdown messages and a
  `/export` endpoint.
- **0.10–0.12 — Roster & resilience.** Duplicate-join protection, token resend
  on re-join, idle-reaped peer revival, ping/status, operator kick, ACK +
  replay on reconnect, agent `talker`/`worker` types and the channel convener.
- **0.13–0.16 — Talking stick & forms.** Floor control across hub, bridge,
  native connector, and console; the operator-form lifecycle end to end;
  `--version` flag and `/version` endpoint.
- **0.17–0.20 — Dashboard & hardening.** The v2 operator dashboard SPA, the
  dashboard WebSocket protocol with auth/RBAC and static asset serving, richer
  peer/health state with per-peer pause, and an opt-in JSONL event log.

[Unreleased]: https://github.com/obeone/caucus-mcp/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/obeone/caucus-mcp/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/obeone/caucus-mcp/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/obeone/caucus-mcp/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/obeone/caucus-mcp/releases/tag/v1.0.0
