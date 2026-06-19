# Contributing to Caucus

Thanks for your interest in improving Caucus. This guide covers the local
setup, the checks your change must pass, and the conventions the project
follows.

## Development setup

Caucus uses [uv](https://docs.astral.sh/uv/) for environments.

```bash
git clone https://github.com/obeone/caucus-mcp.git && cd caucus-mcp
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

The `dev` extra pulls in the tooling and `claude-agent-sdk` (needed for the
native-connector tests). To work on the dashboard, see the "Operator dashboard"
section of the README — Node is a build-time dependency only.

## Checks before you push

All three must pass; CI enforces them.

```bash
ruff check src/
mypy src/        # configured strict
pytest           # unit + integration suite under tests/
```

A legacy end-to-end smoke test is also available:

```bash
python smoke_test.py     # prints "ALL CHECKS PASSED" on success
```

## Conventions

- **Python ≥ 3.10**, line length **88**, `mypy` strict.
- `from __future__ import annotations` at the top of every module; PEP 604
  unions (`X | None`).
- Full NumPy/Google-style docstrings on modules, classes, and functions — match
  the existing density.
- `coloredlogs` for logging. The bridge logs to **stderr** to keep stdout clean
  for the MCP stdio transport — never `print` to stdout there.
- **English only** in code, comments, docstrings, commit messages, and docs.

## Load-bearing invariants

Some constraints are easy to break and hard to debug. Keep them intact:

- **Long-poll ordering**: server poll (`LONG_POLL_SECONDS = 25`) < bridge httpx
  timeout (35s) < client timeout. Invert it and you get spurious disconnects.
- **Watcher starts on `join`, not on first `say`** — a peer may message first;
  with no watcher running, that inbound is never observed.
- **State is in-memory only** — all mutation goes through `HubState` so the
  FastAPI layer stays thin.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full picture.

## Commits and pull requests

- **[Conventional Commits](https://www.conventionalcommits.org/)**, imperative
  mood, no markdown in the message. Prefix the branch to match
  (`feat/`, `fix/`, `docs/`, `chore/`, `test/`, `refactor/`).
- Commit progressively in small, atomic commits; stage selectively.
- Open a pull request against `main`. Make sure the checks above are green and
  describe what changed and how you verified it.

## Versioning

The version (SemVer) is derived from git tags by `hatch-vcs` — there is no
version field to bump and no `chore(release)` commit. To cut a release, create
a GitHub Release `vX.Y.Z`; that tag becomes the version and the `Release`
workflow builds and publishes it. Several PRs can land on `main` between
releases. `caucus.__version__` reads the build-time `_version.py` back.
`PROTOCOL_VERSION` in `hub.py` is a separate counter — bump it only when
`PROTOCOL_TEXT` changes. Record user-visible changes under `## [Unreleased]` in
[`CHANGELOG.md`](CHANGELOG.md); rename that heading to the version on release.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
