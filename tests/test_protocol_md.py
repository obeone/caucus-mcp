"""Drift guard: ``caucus-protocol.md`` must mirror the hub's ``PROTOCOL_TEXT``.

``caucus-protocol.md`` is a human-readable copy deployed into peer repos; it has
no runtime effect, so nothing stops it from describing stale behaviour while the
canonical ``PROTOCOL_TEXT`` (served by the hub) moves on. These tests pin the
load-bearing forms rules to a canonical phrase shared by both documents so the
two cannot silently diverge.
"""

from __future__ import annotations

from pathlib import Path

from caucus.hub import PROTOCOL_TEXT

# tests/ sits at the repo root next to caucus-protocol.md.
_PROTOCOL_MD = Path(__file__).resolve().parent.parent / "caucus-protocol.md"

# The exact sentence the forms-only private-contact rule hangs on. Asserting it
# verbatim in BOTH documents catches a one-sided edit.
_SIGNAL_BEFORE_PRIVATE = "taking this to the operator privately"


def _read_md() -> str:
    return _PROTOCOL_MD.read_text(encoding="utf-8")


def test_protocol_md_documents_forms_tools() -> None:
    text = _read_md()
    assert "ask_operator" in text
    assert "list_forms" in text


def test_protocol_md_shares_signal_before_private_phrase_with_hub() -> None:
    # The canonical sentence must appear in BOTH the hub text and the mirror, so
    # the .md cannot drift to describe stale private-contact behaviour.
    assert _SIGNAL_BEFORE_PRIVATE in PROTOCOL_TEXT
    assert _SIGNAL_BEFORE_PRIVATE in _read_md()


def test_protocol_md_documents_quiet_sign_of_life() -> None:
    text = _read_md()
    assert "signs of life" in text
    assert "quiet" in text
