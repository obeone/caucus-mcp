"""Serialise the hub message log into downloadable transcript formats.

Pure presentation helpers, decoupled from the HTTP layer and from
:class:`~caucus.state.HubState`: each function takes the JSON-friendly message
dicts produced by :meth:`HubState.recent` (i.e. :meth:`Message.to_public`) and
returns a single string. The ``/export`` endpoint wires the chosen format into a
downloadable response; these functions stay trivially unit-testable on their own.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import __version__
from .models import BROADCAST

#: Export formats understood by :func:`render`, mapped to ``(extension,
#: media_type)``. The keys are the accepted ``format`` query values; aliases
#: (e.g. ``md``) are folded onto a canonical key in :func:`normalise_format`.
FORMATS: dict[str, tuple[str, str]] = {
    "json": ("json", "application/json"),
    "markdown": ("md", "text/markdown; charset=utf-8"),
    "text": ("txt", "text/plain; charset=utf-8"),
}

#: Alias -> canonical format key, so ``md``/``txt`` callers land on the right one.
_ALIASES = {"md": "markdown", "txt": "text", "plain": "text"}


def normalise_format(fmt: str) -> str:
    """Fold a caller-supplied format string onto a canonical :data:`FORMATS` key.

    Args:
        fmt: The raw ``format`` query value (case-insensitive); aliases like
            ``md`` and ``txt`` are accepted.

    Returns:
        The canonical key (``"json"``, ``"markdown"`` or ``"text"``), defaulting
        to ``"json"`` for anything unrecognised.
    """
    key = fmt.strip().lower()
    key = _ALIASES.get(key, key)
    return key if key in FORMATS else "json"


def _fmt_ts(ts: float) -> str:
    """Render a Unix timestamp as a local ``YYYY-MM-DD HH:MM:SS`` wall-clock string."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _target(recipient: str) -> str:
    """Human-readable recipient label (``BROADCAST`` shown as ``all``)."""
    return "all" if recipient == BROADCAST else recipient


def to_json(messages: list[dict[str, object]]) -> str:
    """Serialise the log as a pretty-printed JSON document.

    The envelope carries the package ``version`` and a message ``count`` so an
    archived export is self-describing; ``messages`` is the verbatim public form
    of each log entry.

    Args:
        messages: Public message dicts (see :meth:`Message.to_public`).

    Returns:
        A JSON string with a trailing newline.
    """
    payload = {
        "version": __version__,
        "count": len(messages),
        "messages": messages,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def to_markdown(messages: list[dict[str, object]]) -> str:
    """Render the log as a human-readable Markdown transcript.

    Each entry gets a bold attribution line (``sender → target · timestamp``, with
    the kind appended for non-chat notices) followed by its content. Ordinary
    messages are emitted verbatim — agents already write Markdown, so the body
    drops straight in — while system/control notices are shown as a blockquote so
    they read as out-of-band.

    Args:
        messages: Public message dicts (see :meth:`Message.to_public`).

    Returns:
        A Markdown string with a trailing newline.
    """
    lines = ["# Caucus chat export", "", f"_{len(messages)} message(s)_", ""]
    for m in messages:
        sender = str(m["sender"])
        target = _target(str(m["recipient"]))
        kind = str(m.get("kind", "message"))
        ts = _fmt_ts(float(m["ts"]))  # type: ignore[arg-type]
        suffix = "" if kind == "message" else f" · _{kind}_"
        lines.append(f"**{sender}** → {target} · {ts}{suffix}")
        lines.append("")
        content = str(m["content"])
        if kind == "message":
            lines.append(content)
        else:
            # Out-of-band notices read as a quote so they don't masquerade as chat.
            lines.extend(f"> {ln}" for ln in content.splitlines() or [""])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_text(messages: list[dict[str, object]]) -> str:
    """Render the log as a plain-text transcript (no Markdown markup).

    A compact ``[timestamp] sender -> target (kind): content`` line per message,
    suitable for grepping or pasting where Markdown would be noise.

    Args:
        messages: Public message dicts (see :meth:`Message.to_public`).

    Returns:
        A plain-text string with a trailing newline.
    """
    lines: list[str] = []
    for m in messages:
        sender = str(m["sender"])
        target = _target(str(m["recipient"]))
        kind = str(m.get("kind", "message"))
        ts = _fmt_ts(float(m["ts"]))  # type: ignore[arg-type]
        tag = "" if kind == "message" else f" ({kind})"
        content = str(m["content"]).replace("\n", " ")
        lines.append(f"[{ts}] {sender} -> {target}{tag}: {content}")
    return "\n".join(lines) + "\n"


def render(messages: list[dict[str, object]], fmt: str) -> tuple[str, str, str]:
    """Render ``messages`` in ``fmt`` and describe how to deliver it.

    Args:
        messages: Public message dicts (see :meth:`Message.to_public`).
        fmt: A format key or alias (see :func:`normalise_format`).

    Returns:
        A ``(body, media_type, filename)`` triple: the serialised transcript, the
        HTTP ``Content-Type`` to send it with, and a suggested download filename.
    """
    key = normalise_format(fmt)
    extension, media_type = FORMATS[key]
    renderer = {"json": to_json, "markdown": to_markdown, "text": to_text}[key]
    return renderer(messages), media_type, f"caucus-chat.{extension}"
