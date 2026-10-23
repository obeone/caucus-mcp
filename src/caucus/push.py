"""Transport-neutral pieces shared by the connectors that *push* inbound to a host.

Two connectors deliver caucus traffic into a passive host without waking it by a
process exit: the Claude Code channel pump (:mod:`caucus.claude_channel`, inside
the stdio bridge) and the Codex push mode of ``caucus-watch``
(:mod:`caucus.codex_push`). They differ in how a batch reaches the host, and agree
on everything before that, which lives here:

* **settling a burst** (:func:`gather_burst`): once a poll brings a message, keep
  polling and push the burst as one delivery when the hub has been quiet for
  :data:`QUIET_GAP_SECONDS`, or at the latest :func:`settle_seconds` after its
  first message. Operator messages, operator commands, a room stop and a
  displacement end the window at once;
* **rendering** a message or a burst (:func:`to_channel_event`,
  :func:`to_batch_event`): the native connectors' fenced framing, with every
  peer-chosen label flattened by :func:`clean_label` so it cannot forge it.
"""

from __future__ import annotations

import logging
import math
import os
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

import anyio

from .hub_connector import Inbound
from .native_agent import _defang_fence, format_inbound

logger = logging.getLogger("caucus.push")

#: Environment variable holding the settle window, in seconds (``0`` disables).
SETTLE_ENV = "CAUCUS_PUSH_SETTLE"

#: Default settle window: the longest a burst keeps collecting after its first
#: message before it is delivered as one batch.
DEFAULT_SETTLE_SECONDS = 5.0

#: Silence that ends a burst before the window does. Long enough to span the
#: gap between two ``say`` calls a peer makes in one turn (a tool round-trip),
#: short enough that a lone message is not held for the whole window.
QUIET_GAP_SECONDS = 1.5

#: Long-poll ceiling requested from the hub. Must stay at or under the hub's own
#: LONG_POLL_SECONDS (25) and below the connector's HTTP timeout (35): the
#: long-poll ordering invariant in CLAUDE.md.
POLL_TIMEOUT = 25.0

#: Notices for the per-agent operator commands. A pushed host cannot be
#: interrupted or reset from outside, so these reach it as plain text.
COMMAND_NOTICES = {
    "interrupt": (
        "the operator asked you to interrupt: stop the task in progress and wait"
        " for instructions"
    ),
    "reset": (
        "the operator asked for a reset: drop the current caucus task and start"
        " from a clean slate"
    ),
}

# Values that count as "on" for a boolean environment flag.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Longest label (sender, recipient, form title) copied into an event. Project
# names are capped at 64 by the hub; titles at 200.
_LABEL_MAX = 200


def env_flag(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Tell whether a boolean environment flag is on.

    Args:
        name: The variable to read.
        environ: The environment to read. ``None`` reads :data:`os.environ`.

    Returns:
        ``True`` when the variable holds ``1``, ``true``, ``yes`` or ``on`` (any
        case, surrounding whitespace ignored), else ``False``.
    """
    env = os.environ if environ is None else environ
    return env.get(name, "").strip().lower() in _TRUTHY


def settle_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Read the settle window from the environment.

    Args:
        environ: The environment to read. ``None`` reads :data:`os.environ`.

    Returns:
        :data:`SETTLE_ENV` as a float clamped to ``[0, 25]`` (the hub's
        long-poll ceiling), or :data:`DEFAULT_SETTLE_SECONDS` when the variable
        is unset, blank or not a finite number (a typo must not disable
        coalescing or crash the connector; it is logged instead).
    """
    env = os.environ if environ is None else environ
    raw = env.get(SETTLE_ENV, "").strip()
    if not raw:
        return DEFAULT_SETTLE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        logger.warning(
            "%s=%r is not a number; using %.0fs", SETTLE_ENV, raw, DEFAULT_SETTLE_SECONDS
        )
        return DEFAULT_SETTLE_SECONDS
    return min(max(value, 0.0), POLL_TIMEOUT)


def clean_label(value: object) -> str:
    """Flatten a peer-chosen label so it cannot forge the delivery's framing.

    Sender, recipient and form-title strings are chosen by peers, and the hub
    restricts only their length. They end up *outside* the untrusted-data fence
    (the attribution line) and in event metadata, so a newline or an embedded
    fence delimiter there could fake trusted framing. Every control or format
    character becomes a space, whitespace runs collapse, fence delimiters are
    defanged, and the result is capped.

    Args:
        value: The raw label (any type; ``None`` becomes ``"?"``).

    Returns:
        A single-line, non-empty label of at most :data:`_LABEL_MAX` characters.
    """
    text = "?" if value is None else str(value)
    flat = "".join(
        " " if unicodedata.category(ch).startswith(("C", "Z")) else ch for ch in text
    )
    label = _defang_fence(" ".join(flat.split()))[:_LABEL_MAX]
    return label or "?"


def to_channel_event(message: Mapping[str, object]) -> tuple[str, dict[str, str]]:
    """Render one hub message as event ``content`` and ``meta``.

    The content reuses the native connectors' :func:`format_inbound` framing, so
    the body is fenced as untrusted data under an attribution line exactly as a
    native agent sees it. ``meta`` carries the routing facts; every key is an
    identifier (Claude Code silently drops any other key) and every value a
    string. ``source`` is never emitted: Claude Code sets it itself from the
    server name.

    Args:
        message: One message in the hub's public shape (``sender``,
            ``recipient``, ``content``, ``kind``, ``origin``, ``id``, ``seq``,
            optional ``meta``).

    Returns:
        A ``(content, meta)`` pair.
    """
    sender = clean_label(message.get("sender"))
    recipient = clean_label(message.get("recipient"))
    content = format_inbound(
        [{"sender": sender, "recipient": recipient, "content": message.get("content")}]
    )
    meta: dict[str, str] = {
        "sender": sender,
        "recipient": recipient,
        "kind": clean_label(message.get("kind") or "message"),
        "origin": clean_label(message.get("origin") or "agent"),
    }
    if message.get("id") is not None:
        meta["msg_id"] = clean_label(message["id"])
    if message.get("seq") is not None:
        meta["seq"] = clean_label(message["seq"])
    if recipient.startswith("#"):
        meta["hub_channel"] = recipient
    extra = message.get("meta")
    if isinstance(extra, Mapping):
        # Operator-form answers and cancellations: the recap is already in the
        # content, these let the agent match it to the form it opened.
        if extra.get("form_id") is not None:
            meta["form_id"] = clean_label(extra["form_id"])
        if extra.get("title") is not None:
            meta["form_title"] = clean_label(extra["title"])
        if extra.get("status") is not None:
            meta["form_status"] = clean_label(extra["status"])
    return content, meta


def to_batch_event(
    messages: list[dict[str, object]],
) -> tuple[str, dict[str, str]]:
    """Render a settled burst of hub messages as one event.

    The content is one :func:`format_inbound` block holding every message, each
    fenced under its own (cleaned) attribution line. ``meta`` summarises the
    burst, since a tag carries one value per attribute: ``kind="batch"``, the
    message ``count``, and the distinct ``senders``, ``kinds`` and ``origins``
    (comma-separated, in arrival order), plus the ``seq`` range and any operator
    ``form_ids``.

    Args:
        messages: Two or more messages in the hub's public shape, in seq order.

    Returns:
        A ``(content, meta)`` pair.
    """
    senders = [clean_label(m.get("sender")) for m in messages]
    cleaned: list[dict[str, object]] = [
        {
            "sender": sender,
            "recipient": clean_label(m.get("recipient")),
            "content": m.get("content"),
        }
        for sender, m in zip(senders, messages)
    ]

    def distinct(values: list[str]) -> str:
        """Join the distinct values, keeping first-seen order."""
        return ",".join(dict.fromkeys(values))

    meta: dict[str, str] = {
        "kind": "batch",
        "count": str(len(messages)),
        "senders": distinct(senders),
        "kinds": distinct([clean_label(m.get("kind") or "message") for m in messages]),
        "origins": distinct([clean_label(m.get("origin") or "agent") for m in messages]),
    }
    seqs = [clean_label(m["seq"]) for m in messages if m.get("seq") is not None]
    if seqs:
        meta["seq_first"] = seqs[0]
        meta["seq_last"] = seqs[-1]
    forms: list[str] = []
    for m in messages:
        extra = m.get("meta")
        if isinstance(extra, Mapping) and extra.get("form_id") is not None:
            forms.append(clean_label(extra["form_id"]))
    if forms:
        meta["form_ids"] = distinct(forms)
    return format_inbound(cleaned), meta


def render(messages: list[dict[str, object]]) -> tuple[str, dict[str, str]]:
    """Render one message or a burst, whichever ``messages`` holds.

    Args:
        messages: One or more messages in the hub's public shape.

    Returns:
        :func:`to_channel_event` for one message, :func:`to_batch_event` else.
    """
    return to_channel_event(messages[0]) if len(messages) == 1 else to_batch_event(messages)


def max_seq(messages: list[dict[str, object]]) -> int | None:
    """Return the highest ``seq`` in ``messages``, the value to ACK.

    Args:
        messages: Messages in the hub's public shape.

    Returns:
        The highest positive ``seq``, or ``None`` when none carries one.
    """
    seqs = [int(str(m["seq"])) for m in messages if m.get("seq")]
    return max(seqs) if seqs else None


def has_operator(messages: list[dict[str, object]]) -> bool:
    """Tell whether any message came from the operator (server-set ``origin``).

    Args:
        messages: Messages in the hub's public shape.

    Returns:
        ``True`` when at least one carries ``origin == "operator"``.
    """
    return any(m.get("origin") == "operator" for m in messages)


@dataclass(slots=True)
class Burst:
    """What one settled burst collected from the hub.

    Attributes:
        messages: Chatter in seq order (control signals removed).
        commands: Per-agent operator commands (``interrupt``, ``reset``, ...).
        stop: Whether the operator stopped the room during the burst.
        displaced: Whether another listener took the inbox (HTTP 409).
    """

    messages: list[dict[str, object]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    stop: bool = False
    displaced: bool = False

    def absorb(self, inbound: Inbound) -> None:
        """Add one poll's batch to the burst.

        Args:
            inbound: The batch to add.
        """
        self.messages.extend(inbound.messages)
        self.commands.extend(inbound.commands)
        self.stop = self.stop or inbound.stop
        self.displaced = self.displaced or inbound.already_listening

    @property
    def urgent(self) -> bool:
        """Whether the burst must be delivered now, without settling further."""
        return self.stop or self.displaced or bool(self.commands) or has_operator(
            self.messages
        )


async def gather_burst(
    first: Inbound,
    poll: Callable[[float], Awaitable[Inbound | None]],
    *,
    settle: float,
    quiet_gap: float = QUIET_GAP_SECONDS,
    keep_going: Callable[[], bool] = lambda: True,
) -> Burst:
    """Let a burst settle, starting from the batch of a first poll.

    Coalescing only pays off when the host is idle: a host already groups what
    lands while its turn runs, and waking an idle one on the first message of a
    burst would spend one turn on it and a second on the rest. So this
    debounces:

    * after the first message it keeps polling and stops as soon as the hub
      stays quiet for ``quiet_gap`` seconds (the hub's long-poll is the timer:
      a poll that comes back empty *is* the quiet gap);
    * a burst that keeps trickling stops anyway once ``settle`` seconds passed
      since its first message, so latency stays bounded;
    * an urgent batch stops it at once (see :attr:`Burst.urgent`).

    Nothing must be ACKed while a burst settles; the caller ACKs the burst once
    it is delivered. A failing poll ends the window instead of escaping: the
    batch already left the hub queue, and the hub only replays unacked messages
    on a re-register, so letting the error escape would drop it for good.

    Args:
        first: The batch the first poll returned.
        poll: Runs one ``/receive`` with the given timeout, returning ``None``
            when it was cancelled or failed in transit. Must not piggyback an ACK
            for messages of this burst.
        settle: Longest window, in seconds; ``0`` disables settling.
        quiet_gap: Silence that ends the burst early, in seconds.
        keep_going: Checked before each extra poll; ``False`` ends the window.

    Returns:
        Everything collected, in arrival order.
    """
    burst = Burst()
    burst.absorb(first)
    if not burst.messages or settle <= 0 or burst.urgent:
        return burst
    deadline = anyio.current_time() + settle
    while keep_going():
        remaining = deadline - anyio.current_time()
        if remaining <= 0:
            break  # the burst kept trickling: deliver what settled
        try:
            more = await poll(min(quiet_gap, remaining))
        except Exception as exc:  # noqa: BLE001 - any failure, same fix
            logger.warning("poll failed mid-burst (%s); delivering what settled", exc)
            break
        if more is None:
            break
        if not (more.messages or more.commands or more.stop or more.already_listening):
            break  # quiet for a whole gap: the burst is over
        burst.absorb(more)
        if more.stop or more.already_listening or more.commands or has_operator(
            more.messages
        ):
            break
    return burst
