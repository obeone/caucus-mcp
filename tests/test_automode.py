"""Unit tests for :mod:`caucus.automode`.

Covers detection (present / missing / unknown across project-local and user
scopes), the idempotent atomic install into ``.claude/settings.local.json``
(defaults splicing, preservation of existing rules, backup, ``0600`` mode), the
critique gate's graceful degradation when the ``claude`` CLI is absent, and the
``caucus-setup-automode`` CLI surface.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from caucus import automode


@pytest.fixture
def isolated_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the user-baseline settings at an empty temp dir, not the real home.

    Returns the path the module will treat as ``~/.claude/settings.json`` so a
    test can write a user-scope rule into it.
    """
    user_settings = tmp_path / "home" / ".claude" / "settings.json"
    monkeypatch.setattr(automode, "_user_settings_path", lambda: user_settings)
    return user_settings


def _write_json(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path``, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_detect_unknown_when_no_settings(
    tmp_path: Path, isolated_user: Path
) -> None:
    """No settings file in either scope means provenance is undetermined."""
    result = automode.detect(tmp_path)
    assert result["operator_rule"] == "unknown"


def test_detect_missing_when_settings_lack_rule(
    tmp_path: Path, isolated_user: Path
) -> None:
    """A readable settings file without the marker reports ``missing``."""
    _write_json(
        tmp_path / ".claude" / "settings.local.json",
        {"autoMode": {"allow": ["some unrelated rule"]}},
    )
    assert automode.detect(tmp_path)["operator_rule"] == "missing"


def test_detect_present_in_user_scope(
    tmp_path: Path, isolated_user: Path
) -> None:
    """The rule installed in the user baseline is detected for any project."""
    _write_json(isolated_user, {"autoMode": {"allow": [automode.RULE_TEXT]}})
    assert automode.detect(tmp_path)["operator_rule"] == "present"


def test_apply_creates_file_with_defaults_and_rule(
    tmp_path: Path, isolated_user: Path
) -> None:
    """A fresh install splices ``$defaults`` then the rule, and is 0600."""
    out = automode.apply_rule(tmp_path)
    assert out["changed"] is True
    assert out["status"] == "installed"
    assert out["backup"] is None

    settings = tmp_path / ".claude" / "settings.local.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == [
        automode.DEFAULTS_SENTINEL,
        automode.RULE_TEXT,
    ]
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


def test_apply_is_idempotent(tmp_path: Path, isolated_user: Path) -> None:
    """A second install is a no-op keyed on the marker, leaving allow intact."""
    automode.apply_rule(tmp_path)
    settings = tmp_path / ".claude" / "settings.local.json"
    before = settings.read_text(encoding="utf-8")

    second = automode.apply_rule(tmp_path)
    assert second["changed"] is False
    assert second["status"] == "already_present"
    assert settings.read_text(encoding="utf-8") == before


def test_detect_present_after_apply(
    tmp_path: Path, isolated_user: Path
) -> None:
    """Detection sees the rule once it has been applied locally."""
    automode.apply_rule(tmp_path)
    assert automode.detect(tmp_path)["operator_rule"] == "present"


def test_apply_preserves_existing_allow_and_backs_up(
    tmp_path: Path, isolated_user: Path
) -> None:
    """An existing allow list is appended to (no defaults clobber) with a backup."""
    settings = tmp_path / ".claude" / "settings.local.json"
    _write_json(
        settings,
        {"autoMode": {"allow": ["$defaults", "operator's own rule"]}, "other": 1},
    )

    out = automode.apply_rule(tmp_path)
    assert out["changed"] is True
    assert out["backup"] is not None
    assert Path(out["backup"]).exists()

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["other"] == 1  # unrelated keys untouched
    assert data["autoMode"]["allow"] == [
        "$defaults",
        "operator's own rule",
        automode.RULE_TEXT,
    ]


def test_apply_rejects_malformed_json(
    tmp_path: Path, isolated_user: Path
) -> None:
    """A corrupt settings file fails loudly rather than silently overwriting."""
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        automode.apply_rule(tmp_path)


def test_run_critique_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``claude`` is not on PATH the critique degrades, not crashes."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(automode.subprocess, "run", _boom)
    result = automode.run_critique()
    assert result["ran"] is False
    assert "claude" in result["error"].lower()


def test_is_claude_code_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host detection keys on the ``CLAUDECODE=1`` marker, nothing else."""
    monkeypatch.setenv("CLAUDECODE", "1")
    assert automode.is_claude_code() is True
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert automode.is_claude_code() is False
    monkeypatch.setenv("CLAUDECODE", "0")
    assert automode.is_claude_code() is False


def test_main_check_only_does_not_write(
    tmp_path: Path, isolated_user: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default (no ``--apply``) invocation reports and writes nothing."""
    rc = automode.main(["--dir", str(tmp_path)])
    assert rc == 0
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert "operator-answer rule" in capsys.readouterr().out


def test_main_apply_no_critique_installs(
    tmp_path: Path, isolated_user: Path
) -> None:
    """``--apply --no-critique`` installs the rule without invoking the gate."""
    rc = automode.main(["--apply", "--no-critique", "--dir", str(tmp_path)])
    assert rc == 0
    assert automode.detect(tmp_path)["operator_rule"] == "present"
