"""Audit and remove the retired Caucus Claude Code auto-mode rule.

Older Caucus releases offered an ``autoMode.allow`` rule that treated a
rendered message containing ``origin="operator"`` and ``kind="answer"`` as
permission for a bounded action. That trust decision was unsound: Claude
Code's classifier sees model-visible text, not the hub's authenticated
``Message.origin`` field, and a peer can put the same markers in message
content.

Caucus therefore keeps operator answers informational on passive MCP hosts.
Only a host-native approval mechanism that checks structured provenance and
binds one answer to one exact pending action may turn an answer into execution
authority. This module retains the historical setup command solely so upgrades
can detect and remove project-local rules installed by older versions.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from ._private_backup import write_private_backup

logger = logging.getLogger(__name__)

# Stable marker embedded in every rule written by affected older releases.
# Cleanup keys on the marker so it removes stale and reworded variants rather
# than relying on one exact copy of the unsafe prose.
RULE_MARKER = "[caucus-mcp:operator-answer]"


def _read_allow_rules(path: Path) -> list[str] | None:
    """Return the ``autoMode.allow`` rule strings from a settings file.

    Args:
        path: Path to a Claude Code ``settings*.json`` file.

    Returns:
        The string rules (possibly empty) when the file is readable JSON, or
        ``None`` when it is absent, unreadable, or malformed. A missing or
        non-list ``autoMode.allow`` value is represented as an empty list.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("auto-mode audit: %s is not valid JSON; ignoring", path)
        return None
    auto = data.get("autoMode") if isinstance(data, dict) else None
    allow = auto.get("allow") if isinstance(auto, dict) else None
    if not isinstance(allow, list):
        return []
    return [rule for rule in allow if isinstance(rule, str)]


def _local_settings_path(project_dir: Path) -> Path:
    """Return the per-project-per-user settings path for ``project_dir``.

    Args:
        project_dir: Project root containing the ``.claude`` directory.

    Returns:
        The project's ``.claude/settings.local.json`` path.
    """
    return project_dir / ".claude" / "settings.local.json"


def _user_settings_path() -> Path:
    """Return the user-baseline Claude Code settings path."""
    return Path.home() / ".claude" / "settings.json"


def is_claude_code() -> bool:
    """Return whether the current process is running under Claude Code."""
    return os.environ.get("CLAUDECODE") == "1"


def detect(project_dir: Path | None = None) -> dict[str, object]:
    """Report whether an unsafe legacy Caucus allow rule remains active.

    The audit is side-effect free and checks both settings scopes read by
    Claude Code. The historical ``operator_rule`` key remains in the response
    for bridge compatibility, but its only value is now ``"retired"``.

    Args:
        project_dir: Project root to inspect. Defaults to the current directory.

    Returns:
        A mapping containing the retired feature status, legacy-rule status,
        affected scopes, marker, and remediation guidance.
    """
    root = project_dir or Path.cwd()
    local = _read_allow_rules(_local_settings_path(root))
    user = _read_allow_rules(_user_settings_path())

    legacy_scopes: list[str] = []
    if local is not None and any(RULE_MARKER in rule for rule in local):
        legacy_scopes.append("project")
    if user is not None and any(RULE_MARKER in rule for rule in user):
        legacy_scopes.append("user")

    if legacy_scopes:
        legacy_status = "present"
        remediation = (
            " Remove every autoMode.allow entry containing "
            f"{RULE_MARKER!r}. `caucus-setup-automode --apply` removes the "
            "project-local entry; a user-scope entry must be removed from "
            "~/.claude/settings.json."
        )
    elif local is None and user is None:
        legacy_status = "unknown"
        remediation = " No readable Claude Code settings file was found."
    else:
        legacy_status = "absent"
        remediation = " No legacy Caucus allow rule was found."

    return {
        "operator_rule": "retired",
        "legacy_rule": legacy_status,
        "legacy_scopes": legacy_scopes,
        "marker": RULE_MARKER,
        "hint": (
            "Caucus operator form answers are informational on passive MCP "
            "hosts and never provide tool-execution authority."
            + remediation
        ),
    }


def _atomic_write_json(path: Path, data: dict[str, object]) -> None:
    """Write ``data`` to ``path`` atomically with ``0600`` permissions.

    Args:
        path: Destination settings file.
        data: JSON-serialisable settings mapping.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".settings.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def apply_rule(project_dir: Path | None = None) -> dict[str, object]:
    """Remove unsafe legacy Caucus rules from project-local settings.

    The name is retained for callers of the historical API. It no longer
    installs any rule. Every string entry containing :data:`RULE_MARKER` is
    removed, while unrelated rules and settings are preserved. A backup is
    written only when cleanup changes the file.

    Args:
        project_dir: Project root to clean. Defaults to the current directory.

    Returns:
        A mapping with ``changed``, ``status`` (``"removed"`` or
        ``"not_present"``), settings ``path``, optional ``backup``, and the
        number of removed entries.

    Raises:
        RuntimeError: If an existing settings file cannot be read as a JSON
            object. Unsafe state is never silently replaced.
    """
    root = project_dir or Path.cwd()
    path = _local_settings_path(root)
    if not path.exists():
        return {
            "changed": False,
            "status": "not_present",
            "path": str(path),
            "backup": None,
            "removed": 0,
        }

    try:
        raw = path.read_text(encoding="utf-8")
        loaded: object = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("settings root is not a JSON object")  # noqa: TRY004
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot parse {path}: {exc}") from exc

    data: dict[str, object] = loaded
    auto_raw = data.get("autoMode")
    if not isinstance(auto_raw, dict):
        return {
            "changed": False,
            "status": "not_present",
            "path": str(path),
            "backup": None,
            "removed": 0,
        }
    auto: dict[str, object] = auto_raw
    allow = auto.get("allow")
    if not isinstance(allow, list):
        return {
            "changed": False,
            "status": "not_present",
            "path": str(path),
            "backup": None,
            "removed": 0,
        }

    filtered = [
        rule
        for rule in allow
        if not (isinstance(rule, str) and RULE_MARKER in rule)
    ]
    removed = len(allow) - len(filtered)
    if removed == 0:
        return {
            "changed": False,
            "status": "not_present",
            "path": str(path),
            "backup": None,
            "removed": 0,
        }

    auto["allow"] = filtered
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        write_private_backup(backup, raw)
    except OSError as exc:
        raise RuntimeError(f"cannot safely back up {path}: {exc}") from exc
    _atomic_write_json(path, data)
    return {
        "changed": True,
        "status": "removed",
        "path": str(path),
        "backup": str(backup),
        "removed": removed,
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``caucus-setup-automode`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="caucus-setup-automode",
        description=(
            "Audit for the retired Caucus operator-answer auto-mode rule and "
            "remove an unsafe project-local legacy copy."
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
        help="Remove the unsafe legacy rule from project-local settings.",
    )
    # Accept historical flags so old remediation commands fail safe instead of
    # failing argument parsing. They cannot restore installation behavior.
    parser.add_argument("--no-critique", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--model", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Audit or remove the retired auto-mode rule.

    Check-only remains the default. ``--apply`` removes project-local legacy
    entries and then audits both project and user scopes. A remaining user-wide
    entry produces a nonzero exit so automation cannot mistake partial cleanup
    for a safe state.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when the audit is safe, otherwise ``1``.
    """
    args = _build_parser().parse_args(argv)
    root = args.dir or Path.cwd()

    if not args.apply:
        audit = detect(root)
        result: dict[str, object] = {"action": "check", **audit}
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Caucus operator-answer auto-mode rule: retired")
            print(f"Unsafe legacy rule: {audit['legacy_rule']}")
            print(audit["hint"])
        return 1 if audit["legacy_rule"] == "present" else 0

    try:
        cleanup = apply_rule(root)
    except RuntimeError as exc:
        if args.json:
            print(json.dumps({"action": "cleanup", "error": str(exc)}, indent=2))
        else:
            print(f"Failed to remove legacy rule: {exc}", file=sys.stderr)
        return 1

    audit = detect(root)
    result = {"action": "cleanup", **cleanup, "audit": audit}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if cleanup["changed"]:
            print(
                f"Removed {cleanup['removed']} unsafe legacy rule(s) from "
                f"{cleanup['path']}."
            )
            print(f"Previous file backed up to {cleanup['backup']}.")
        else:
            print(f"No project-local legacy rule found in {cleanup['path']}.")
        print(audit["hint"])
    return 1 if audit["legacy_rule"] == "present" else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
