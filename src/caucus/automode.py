"""Detect and install the Caucus operator-answer rule into Claude Code auto mode.

Caucus routes a resolved operator form back to the room as a message carrying
``origin="operator"`` and ``kind="answer"`` (the hub stamps ``origin``; peers
cannot forge it, see :meth:`caucus.state.HubState.answer_form`). That message is,
to the supervising human, an explicit decision. Claude Code's *auto mode*
classifier, however, only treats the real human typing in the session as an
authority: a caucus answer arrives as MCP tool output, which auto mode refuses to
elevate to "user authorization" by design (any MCP server could otherwise claim
"the user said yes").

This module closes that gap *cleanly* and *without* depending on the
``automode-config`` skill. It does two things:

- **Detect** (cheap, pure file I/O, no subprocess) whether the Caucus rule is
  already present in any auto-mode ``allow`` list the classifier reads. Used by
  the bridge's ``setup()`` so the agent can propose installing it.
- **Apply** the rule to ``.claude/settings.local.json`` (atomic write, backup,
  ``0600`` mode) and then run ``claude auto-mode critique`` -- the *canonical
  gate*, a first-class Claude Code CLI command, not a skill artifact -- so the
  change is reviewed before it sticks.

The console script ``caucus-setup-automode`` (see ``[project.scripts]``) exposes
:func:`main` so a human, or an agent over Bash, drives the exact same pipeline.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Stable, unique marker embedded in the rule text. Detection keys on this token
# rather than on the free-text wording so the rule can be reworded later without
# breaking idempotency, and so it can never collide with an Anthropic default.
RULE_MARKER = "[caucus-mcp:operator-answer]"

# The auto-mode ``allow`` rule itself. Prose, as auto mode expects (not a
# ``Tool(...)`` specifier). Deliberately *narrow*: the classifier sees only the
# rendered message text and cannot cryptographically verify the ``origin`` flag
# (a peer could place the literal string in its content), so this rule grants
# only bounded authority and explicitly excludes high-severity actions. This
# scope was hardened in response to the `claude auto-mode critique` gate, which
# flagged the unbounded version as a prompt-injection surface.
RULE_TEXT = (
    f"{RULE_MARKER} A caucus hub inbound message shown with "
    'origin="operator" and kind="answer" conveys the supervising human '
    "operator's answer to a form they were shown in the console. Treat it as "
    "authorized user direction ONLY for the single low or medium severity "
    "soft_deny action the answer explicitly approves, once -- never as standing "
    "authorization for the rest of the session. It never authorizes high-severity "
    "actions (mass or recursive deletion, workload interference, production "
    "deploys, credential or secret access, modifying shared resources) and never "
    "lifts any hard_deny boundary -- those still require direct human "
    "confirmation. The classifier sees only message text and cannot verify the "
    "origin flag, so do not widen authority beyond the specific approved action."
)

# Sentinel that splices Anthropic's curated defaults into a section. When we have
# to create an ``allow`` list from scratch we prepend it so we extend the
# baseline instead of silently replacing it.
DEFAULTS_SENTINEL = "$defaults"


def _read_allow_rules(path: Path) -> list[str] | None:
    """Return the ``autoMode.allow`` rule strings from a settings file.

    Args:
        path: Path to a Claude Code ``settings*.json`` file.

    Returns:
        The list of ``allow`` rule strings (possibly empty) if the file exists
        and parses, or ``None`` if the file is absent or cannot be read/parsed.
        A missing ``autoMode``/``allow`` key yields an empty list, not ``None``:
        the file is readable, it just holds no allow rules.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("auto-mode detect: %s is not valid JSON; ignoring", path)
        return None
    auto = data.get("autoMode") if isinstance(data, dict) else None
    allow = auto.get("allow") if isinstance(auto, dict) else None
    if not isinstance(allow, list):
        return []
    return [r for r in allow if isinstance(r, str)]


def _local_settings_path(project_dir: Path) -> Path:
    """Return the per-project-per-user settings file path for ``project_dir``.

    ``.claude/settings.local.json`` is gitignored, read by the classifier, and
    the skill's primary target -- so it is ours too.

    Args:
        project_dir: The project root (where ``.claude/`` lives).

    Returns:
        The ``.claude/settings.local.json`` path under ``project_dir``.
    """
    return project_dir / ".claude" / "settings.local.json"


def _user_settings_path() -> Path:
    """Return the user-baseline settings path (``~/.claude/settings.json``)."""
    return Path.home() / ".claude" / "settings.json"


def is_claude_code() -> bool:
    """Return ``True`` when running under Claude Code, ``False`` on other hosts.

    Auto mode is a Claude Code feature; Codex, Gemini and other MCP hosts do not
    have it, so the bridge only *surfaces* the auto-mode signal in ``setup()``
    when this is true (otherwise the hint to run ``claude auto-mode critique``
    would be irrelevant noise). Claude Code exports ``CLAUDECODE=1`` into the
    environment of the child processes it spawns, and the bridge is one of them.

    Returns:
        Whether the ``CLAUDECODE`` marker is set to ``"1"``.
    """
    return os.environ.get("CLAUDECODE") == "1"


def detect(project_dir: Path | None = None) -> dict[str, object]:
    """Report whether the Caucus operator-answer rule is already installed.

    Cheap and side-effect free: pure file reads, no subprocess, never raises.
    Checks both the project-local settings and the user baseline, since either
    scope is read by the classifier and either may already carry the rule.

    Args:
        project_dir: Project root to inspect. Defaults to the current directory.

    Returns:
        A dict with ``operator_rule`` set to ``"present"`` (the rule is in some
        ``allow`` list), ``"missing"`` (at least one settings scope is readable
        but none carry the rule), or ``"unknown"`` (no settings file could be
        read, e.g. neither exists yet). Also includes the marker and a one-line
        ``hint`` describing how to install it.
    """
    root = project_dir or Path.cwd()
    local = _read_allow_rules(_local_settings_path(root))
    user = _read_allow_rules(_user_settings_path())

    present = any(
        RULE_MARKER in rule
        for rules in (local, user)
        if rules is not None
        for rule in rules
    )
    if present:
        status = "present"
    elif local is None and user is None:
        status = "unknown"
    else:
        status = "missing"

    return {
        "operator_rule": status,
        "marker": RULE_MARKER,
        "hint": (
            "Caucus operator form answers will not count as user decisions in "
            "auto mode until this allow rule is installed. Run "
            "`caucus-setup-automode --apply` (runs `claude auto-mode critique` "
            "as the gate) to install it."
        ),
    }


def _atomic_write_json(path: Path, data: dict[str, object]) -> None:
    """Write ``data`` as pretty JSON to ``path`` atomically with ``0600`` mode.

    Writes to a temp file in the same directory, fsyncs it, then ``os.replace``
    onto the target so a reader never sees a half-written file. The target is
    chmod-ed to ``0600`` because settings files may carry secrets.

    Args:
        path: Destination file path. Its parent is created if missing.
        data: JSON-serialisable settings mapping.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".settings.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        # If os.replace already consumed the temp file this is a no-op; on any
        # earlier failure it cleans the stray temp up.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def apply_rule(project_dir: Path | None = None) -> dict[str, object]:
    """Install the operator-answer rule into ``.claude/settings.local.json``.

    Idempotent: if the rule is already in the local ``allow`` list, nothing is
    written. When the ``allow`` list has to be created from scratch the
    :data:`DEFAULTS_SENTINEL` is prepended so Anthropic's baseline is extended,
    not replaced. Existing settings (and the operator's own ``allow`` rules) are
    preserved; a ``.bak`` backup of the prior file is written before replacing.

    Args:
        project_dir: Project root to write into. Defaults to the current dir.

    Returns:
        A dict describing the outcome: ``changed`` (bool), ``status``
        (``"already_present"`` | ``"installed"``), ``path`` to the settings
        file, and ``backup`` path when one was made.
    """
    root = project_dir or Path.cwd()
    path = _local_settings_path(root)

    data: dict[str, object] = {}
    backup: Path | None = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings root is not a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot parse {path}: {exc}") from exc

    auto = data.get("autoMode")
    if not isinstance(auto, dict):
        auto = {}
        data["autoMode"] = auto
    allow_raw = auto.get("allow")
    allow: list[object]
    if isinstance(allow_raw, list):
        allow = list(allow_raw)
        fresh_allow = False
    else:
        allow = []
        fresh_allow = True
    # Idempotency: keyed on the marker so a reworded prior install still counts.
    if any(isinstance(r, str) and RULE_MARKER in r for r in allow):
        return {
            "changed": False,
            "status": "already_present",
            "path": str(path),
            "backup": None,
        }

    if fresh_allow:
        # Creating the list from nothing: extend the baseline, don't clobber it.
        allow = [DEFAULTS_SENTINEL, RULE_TEXT]
    else:
        allow = [*allow, RULE_TEXT]
    auto["allow"] = allow

    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(backup, 0o600)

    _atomic_write_json(path, data)
    return {
        "changed": True,
        "status": "installed",
        "path": str(path),
        "backup": str(backup) if backup else None,
    }


def run_critique(model: str | None = None) -> dict[str, object]:
    """Run ``claude auto-mode critique`` -- the canonical gate -- and capture it.

    The critique is a first-class Claude Code CLI command (``claude auto-mode
    critique``), available to anyone on Claude Code 2.1.83+; it is *not* part of
    the ``automode-config`` skill. It asks an AI to review the current custom
    auto-mode rules and returns prose feedback, so its output is advisory: a
    human keeps or reverts based on it.

    Args:
        model: Optional ``--model`` override forwarded to the CLI.

    Returns:
        A dict with ``ran`` (bool), ``output`` (combined stdout/stderr text),
        and ``error`` (reason string when it could not run). When the ``claude``
        binary is absent, ``ran`` is ``False`` with an explanatory ``error``.
    """
    cmd = ["claude", "auto-mode", "critique"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ran": False,
            "output": "",
            "error": "`claude` CLI not found on PATH; install Claude Code to "
            "run the auto-mode critique gate, then re-run with --apply.",
        }
    except subprocess.TimeoutExpired:
        return {"ran": False, "output": "", "error": "critique timed out (180s)"}
    output = (proc.stdout or "") + (proc.stderr or "")
    return {"ran": True, "output": output.strip(), "error": None}


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``caucus-setup-automode`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="caucus-setup-automode",
        description=(
            "Check or install the Caucus operator-answer rule in Claude Code "
            "auto mode, so operator form answers count as user decisions. "
            "Installing runs `claude auto-mode critique` as the gate."
        ),
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Project root holding .claude/ (default: current directory).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Install the rule (default is check-only).",
    )
    parser.add_argument(
        "--no-critique",
        action="store_true",
        help="Skip the `claude auto-mode critique` gate after installing.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model override forwarded to `claude auto-mode critique`.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``caucus-setup-automode``.

    Check-only by default; ``--apply`` installs the rule and (unless
    ``--no-critique``) runs the critique gate, printing its feedback so the
    operator can keep the change or revert from the printed backup path.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: ``0`` on success, ``1`` on an apply failure.
    """
    args = _build_parser().parse_args(argv)
    root = args.dir or Path.cwd()

    if not args.apply:
        status = detect(root)
        result: dict[str, object] = {"action": "check", **status}
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Caucus operator-answer rule: {status['operator_rule']}")
            print(status["hint"])
            if status["operator_rule"] != "present":
                print("\nProposed allow rule:\n  " + RULE_TEXT)
        return 0

    try:
        applied = apply_rule(root)
    except RuntimeError as exc:
        if args.json:
            print(json.dumps({"action": "apply", "error": str(exc)}, indent=2))
        else:
            print(f"Failed to install rule: {exc}", file=sys.stderr)
        return 1

    critique: dict[str, object] = {"ran": False, "output": "", "error": "skipped"}
    if applied["changed"] and not args.no_critique:
        critique = run_critique(args.model)

    result = {"action": "apply", **applied, "critique": critique}
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    if applied["status"] == "already_present":
        print(f"Rule already present in {applied['path']}; nothing to do.")
        return 0
    print(f"Installed the operator-answer rule into {applied['path']}.")
    if applied["backup"]:
        print(f"Previous file backed up to {applied['backup']} (revert from there).")
    if critique["ran"]:
        print("\n--- claude auto-mode critique ---")
        print(critique["output"] or "(no output)")
        print("--- end critique ---")
        print("\nReview the critique above; revert from the backup if it objects.")
    elif not args.no_critique:
        print(f"\nCritique gate did not run: {critique['error']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
