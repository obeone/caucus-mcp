"""Unit tests for the retired Claude Code auto-mode authorization rule.

The classifier only sees rendered MCP text, so it cannot authenticate the
hub's structured ``origin`` field. The setup helper must therefore never add
an authority-granting rule. It remains available to find and remove rules
installed by older Caucus releases without disturbing unrelated settings.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from caucus import automode


@pytest.fixture
def isolated_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the user-baseline settings at an empty temporary directory."""
    user_settings = tmp_path / "home" / ".claude" / "settings.json"
    monkeypatch.setattr(automode, "_user_settings_path", lambda: user_settings)
    return user_settings


def _write_json(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path``, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _legacy_rule(suffix: str = "grant the requested action") -> str:
    """Return a representative rule installed by an older release."""
    return f"{automode.RULE_MARKER} {suffix}"


def test_detect_unknown_when_no_settings(
    tmp_path: Path, isolated_user: Path
) -> None:
    """No readable settings scope leaves legacy-rule presence unknown."""
    result = automode.detect(tmp_path)
    assert result["operator_rule"] == "retired"
    assert result["legacy_rule"] == "unknown"
    assert "informational" in str(result["hint"])


def test_detect_absent_when_settings_lack_legacy_rule(
    tmp_path: Path, isolated_user: Path
) -> None:
    """A readable settings file without the marker is safe."""
    _write_json(
        tmp_path / ".claude" / "settings.local.json",
        {"autoMode": {"allow": ["some unrelated rule"]}},
    )
    result = automode.detect(tmp_path)
    assert result["operator_rule"] == "retired"
    assert result["legacy_rule"] == "absent"


def test_detect_marks_user_scope_legacy_rule_unsafe(
    tmp_path: Path, isolated_user: Path
) -> None:
    """Any marker-bearing user rule is reported as unsafe, regardless of prose."""
    _write_json(isolated_user, {"autoMode": {"allow": [_legacy_rule()]}})
    result = automode.detect(tmp_path)
    assert result["operator_rule"] == "retired"
    assert result["legacy_rule"] == "present"
    assert result["legacy_scopes"] == ["user"]
    assert "remove" in str(result["hint"]).lower()


def test_apply_on_clean_project_never_creates_an_allow_rule(
    tmp_path: Path, isolated_user: Path
) -> None:
    """The former install entry point is a safe no-op on a clean project."""
    out = automode.apply_rule(tmp_path)
    assert out["changed"] is False
    assert out["status"] == "not_present"
    assert out["backup"] is None
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_apply_removes_all_legacy_rules_and_preserves_unrelated_settings(
    tmp_path: Path, isolated_user: Path
) -> None:
    """Cleanup removes every marker variant and retains operator-owned rules."""
    settings = tmp_path / ".claude" / "settings.local.json"
    original = {
        "autoMode": {
            "allow": [
                "$defaults",
                _legacy_rule(),
                "operator's own rule",
                _legacy_rule("alternate stale wording"),
            ]
        },
        "other": 1,
    }
    _write_json(settings, original)

    out = automode.apply_rule(tmp_path)

    assert out["changed"] is True
    assert out["status"] == "removed"
    assert out["backup"] is not None
    backup = Path(str(out["backup"]))
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {
        "autoMode": {"allow": ["$defaults", "operator's own rule"]},
        "other": 1,
    }
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


def test_apply_refuses_symlink_backup_without_disclosing_settings(
    tmp_path: Path, isolated_user: Path
) -> None:
    """A prepared backup symlink cannot redirect the original settings."""
    settings = tmp_path / ".claude" / "settings.local.json"
    original = {
        "autoMode": {"allow": [_legacy_rule()]},
        "secret": "victim-only",
    }
    _write_json(settings, original)
    original_text = settings.read_text(encoding="utf-8")
    attacker_target = tmp_path / "attacker-owned"
    attacker_target.write_text("sentinel", encoding="utf-8")
    backup = settings.with_suffix(settings.suffix + ".bak")
    backup.symlink_to(attacker_target)

    with pytest.raises(RuntimeError, match="safely back up"):
        automode.apply_rule(tmp_path)

    assert settings.read_text(encoding="utf-8") == original_text
    assert backup.is_symlink()
    assert attacker_target.read_text(encoding="utf-8") == "sentinel"


def test_apply_safely_refreshes_owned_regular_backup(
    tmp_path: Path, isolated_user: Path
) -> None:
    """A prior owner-controlled backup is rotated to the latest original."""
    settings = tmp_path / ".claude" / "settings.local.json"
    original = {"autoMode": {"allow": [_legacy_rule()]}, "other": 1}
    _write_json(settings, original)
    backup = settings.with_suffix(settings.suffix + ".bak")
    backup.write_text("stale", encoding="utf-8")

    automode.apply_rule(tmp_path)

    info = backup.stat()
    assert stat.S_ISREG(info.st_mode)
    assert info.st_uid == os.geteuid()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert json.loads(backup.read_text(encoding="utf-8")) == original


def test_main_apply_reports_unsafe_backup_as_json(
    tmp_path: Path,
    isolated_user: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Machine-readable cleanup reports a refused backup without a traceback."""
    settings = tmp_path / ".claude" / "settings.local.json"
    original = {"autoMode": {"allow": [_legacy_rule()]}, "secret": "victim-only"}
    _write_json(settings, original)
    original_text = settings.read_text(encoding="utf-8")
    attacker_target = tmp_path / "attacker-owned"
    attacker_target.write_text("sentinel", encoding="utf-8")
    settings.with_suffix(settings.suffix + ".bak").symlink_to(attacker_target)

    rc = automode.main(["--apply", "--json", "--dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out)["error"].startswith("cannot safely back up")
    assert captured.err == ""
    assert settings.read_text(encoding="utf-8") == original_text
    assert attacker_target.read_text(encoding="utf-8") == "sentinel"


def test_apply_is_idempotent_after_cleanup(
    tmp_path: Path, isolated_user: Path
) -> None:
    """A second cleanup does not rewrite already-safe settings."""
    settings = tmp_path / ".claude" / "settings.local.json"
    _write_json(settings, {"autoMode": {"allow": [_legacy_rule()]}})
    automode.apply_rule(tmp_path)
    before = settings.read_text(encoding="utf-8")

    second = automode.apply_rule(tmp_path)

    assert second["changed"] is False
    assert second["status"] == "not_present"
    assert second["backup"] is None
    assert settings.read_text(encoding="utf-8") == before


def test_apply_rejects_malformed_json(
    tmp_path: Path, isolated_user: Path
) -> None:
    """A corrupt settings file fails loudly rather than being overwritten."""
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        automode.apply_rule(tmp_path)


def test_is_claude_code_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host detection keys on the ``CLAUDECODE=1`` marker, nothing else."""
    monkeypatch.setenv("CLAUDECODE", "1")
    assert automode.is_claude_code() is True
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert automode.is_claude_code() is False
    monkeypatch.setenv("CLAUDECODE", "0")
    assert automode.is_claude_code() is False


def test_main_check_only_does_not_write_or_propose_authority(
    tmp_path: Path,
    isolated_user: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Check-only reports retirement and never proposes an allow rule."""
    rc = automode.main(["--dir", str(tmp_path)])
    assert rc == 0
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    output = capsys.readouterr().out
    assert "retired" in output
    assert "informational" in output
    assert "Proposed allow rule" not in output


def test_main_apply_no_critique_cannot_install_authority_rule(
    tmp_path: Path, isolated_user: Path
) -> None:
    """The legacy bypass flag cannot make ``--apply`` install a rule."""
    rc = automode.main(["--apply", "--no-critique", "--dir", str(tmp_path)])
    assert rc == 0
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert automode.detect(tmp_path)["operator_rule"] == "retired"


def test_main_apply_fails_closed_when_user_scope_rule_remains(
    tmp_path: Path, isolated_user: Path
) -> None:
    """Project cleanup cannot claim success while a user-wide rule is active."""
    _write_json(isolated_user, {"autoMode": {"allow": [_legacy_rule()]}})

    rc = automode.main(["--apply", "--dir", str(tmp_path)])

    assert rc == 1
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
