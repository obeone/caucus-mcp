"""Drift guard: ``caucus-protocol.md`` must mirror the hub's ``PROTOCOL_TEXT``.

``caucus-protocol.md`` is a human-readable copy deployed into peer repos; it has
no runtime effect, so nothing stops it from describing stale behaviour while the
canonical ``PROTOCOL_TEXT`` (served by the hub) moves on. These tests pin the
load-bearing forms rules to a canonical phrase shared by both documents so the
two cannot silently diverge.
"""

from __future__ import annotations

from pathlib import Path

from caucus.hub import PROTOCOL_SECTIONS, PROTOCOL_TEXT

# tests/ sits at the repo root next to caucus-protocol.md.
_PROTOCOL_MD = Path(__file__).resolve().parent.parent / "caucus-protocol.md"

# The exact sentence the forms-only private-contact rule hangs on. Asserting it
# verbatim in BOTH documents catches a one-sided edit.
_SIGNAL_BEFORE_PRIVATE = "taking this to the operator privately"

# Revision 24's channel-history warning hangs on this exact phrase. Asserting
# it verbatim in BOTH documents catches a one-sided edit, the same guard as
# _SIGNAL_BEFORE_PRIVATE above.
_CHANNEL_NO_HISTORY = "A channel has NO history"


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


def test_protocol_md_shares_channel_no_history_phrase_with_hub() -> None:
    # Revision 24: a channel has no history, so a message sent into an empty
    # one is lost, not left behind. Both documents must say so in the same
    # words or the mirror can drift back to describing it as a mere quirk.
    assert _CHANNEL_NO_HISTORY in PROTOCOL_TEXT
    assert _CHANNEL_NO_HISTORY in _read_md()


def test_protocol_md_documents_quiet_sign_of_life() -> None:
    text = _read_md()
    assert "signs of life" in text
    assert "quiet" in text


def test_protocol_md_names_the_same_on_demand_sections_as_the_hub() -> None:
    """Revision 19 split the protocol into a core plus fetchable sections.

    Both documents must name the same set: a mirror that advertises a section
    the hub does not serve sends a peer repo's reader after nothing, and one
    that omits a section hides a flow's mechanics entirely.

    Both sides are asserted through the ``protocol_section("<name>")`` call
    form. A bare mention of the name would pass while telling the reader nothing
    about how to obtain the section — the mirror's own table lists every name in
    a column, so a looser check here would be satisfied by that table alone and
    would never notice a section whose prose pointer went missing.
    """
    text = _read_md()
    for name in PROTOCOL_SECTIONS:
        assert f'protocol_section("{name}")' in PROTOCOL_TEXT
        assert f'protocol_section("{name}")' in text
