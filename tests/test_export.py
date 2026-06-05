"""Unit tests for the transcript export helpers.

These exercise :mod:`caucus.export` in isolation (no HTTP layer) over the public
message-dict shape produced by :meth:`caucus.models.Message.to_public`.
"""

from __future__ import annotations

import json

from caucus import __version__
from caucus import export


def _log() -> list[dict[str, object]]:
    """A small, representative log: a chat message, a broadcast, and a notice."""
    return [
        {
            "id": "1",
            "sender": "alpha",
            "recipient": "beta",
            "content": "Ship **v2** of `items`?",
            "kind": "message",
            "ts": 1_700_000_000.0,
        },
        {
            "id": "2",
            "sender": "beta",
            "recipient": "all",
            "content": "Agreed, deploying now.",
            "kind": "message",
            "ts": 1_700_000_005.0,
        },
        {
            "id": "3",
            "sender": "hub",
            "recipient": "all",
            "content": "control: stopped",
            "kind": "system",
            "ts": 1_700_000_010.0,
        },
    ]


def test_normalise_format_folds_aliases_and_defaults() -> None:
    assert export.normalise_format("md") == "markdown"
    assert export.normalise_format("MARKDOWN") == "markdown"
    assert export.normalise_format("txt") == "text"
    assert export.normalise_format("json") == "json"
    assert export.normalise_format("nonsense") == "json"


def test_to_json_is_self_describing() -> None:
    payload = json.loads(export.to_json(_log()))
    assert payload["version"] == __version__
    assert payload["count"] == 3
    assert payload["messages"][0]["content"] == "Ship **v2** of `items`?"


def test_to_markdown_keeps_chat_verbatim_and_quotes_notices() -> None:
    md = export.to_markdown(_log())
    assert md.startswith("# Caucus chat export")
    assert "_3 message(s)_" in md
    # Chat content is left untouched so its Markdown survives the round trip.
    assert "Ship **v2** of `items`?" in md
    # Broadcast recipient renders as "all".
    assert "**beta** → all" in md
    # System notices are tagged and quoted, not passed off as chat.
    assert "· _system_" in md
    assert "> control: stopped" in md


def test_to_text_is_one_flat_line_per_message() -> None:
    text = export.to_text(_log())
    lines = text.splitlines()
    assert len(lines) == 3
    # Timestamps render in the host's local timezone, so assert structure, not a
    # fixed wall-clock string.
    assert lines[0].endswith("alpha -> beta: Ship **v2** of `items`?")
    assert lines[2].endswith("hub -> all (system): control: stopped")


def test_render_dispatches_format_and_filename() -> None:
    _, media, filename = export.render(_log(), "md")
    assert media.startswith("text/markdown")
    assert filename == "caucus-chat.md"

    body, media, filename = export.render(_log(), "json")
    assert media == "application/json"
    assert filename == "caucus-chat.json"
    assert json.loads(body)["count"] == 3

    _, media, filename = export.render(_log(), "weird")  # unknown -> json
    assert media == "application/json"
    assert filename == "caucus-chat.json"
