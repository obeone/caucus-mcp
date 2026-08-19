"""Unit tests for the native Claude connector's pure logic and loop control.

The SDK-bound pieces (``ClaudeSDKClient``, the in-process tools) are integration
surface; here we test the parts that carry the behaviour and need no live model:
prompt composition, inbound formatting, assistant-text extraction, and the
listen → inject → reply control flow driven against lightweight fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from caucus import claude_agent
from caucus import hub as hub_module
from caucus.hub_connector import HubConnector, Inbound


# --- tool policy ---------------------------------------------------------


def test_tool_policy_talker_is_caucus_only() -> None:
    """A talker may use only caucus tools and is blocked from every built-in."""
    allowed, disallowed = claude_agent.tool_policy("talker")
    assert allowed == claude_agent._CAUCUS_TOOLS
    assert disallowed == claude_agent._BUILTIN_TOOLS
    assert "Bash" not in allowed
    assert "Bash" in disallowed


def test_tool_policy_worker_adds_builtins_and_blocks_nothing() -> None:
    """A worker keeps caucus tools and additionally wields the built-ins."""
    allowed, disallowed = claude_agent.tool_policy("worker")
    assert disallowed == []
    for caucus_tool in claude_agent._CAUCUS_TOOLS:
        assert caucus_tool in allowed
    assert "Bash" in allowed
    assert "Edit" in allowed


def test_tool_policy_rejects_unknown_type() -> None:
    """An unknown profile name is a hard error, not a silent talker fallback."""
    with pytest.raises(ValueError, match="unknown agent type"):
        claude_agent.tool_policy("hacker")


# --- pure helpers --------------------------------------------------------


def test_compose_system_prompt_embeds_runtime_framing_and_protocol() -> None:
    prompt = claude_agent.compose_system_prompt("planner", "PROTOCOL BODY")
    assert '"planner"' in prompt
    assert "native Claude connector" in prompt
    assert "listens continuously" in prompt
    assert "PROTOCOL BODY" in prompt


def test_compose_system_prompt_includes_channel_directory() -> None:
    prompt = claude_agent.compose_system_prompt(
        "planner",
        "PROTOCOL BODY",
        {"#api-shape": {"topic": "Designing the API", "members": ["builder"]}},
    )
    assert "[caucus channels]" in prompt
    assert "#api-shape" in prompt
    assert "Designing the API" in prompt
    assert "builder" in prompt


def test_compose_system_prompt_omits_directory_when_no_channels() -> None:
    assert "[caucus channels]" not in claude_agent.compose_system_prompt(
        "planner", "PROTOCOL BODY", {}
    )
    assert "[caucus channels]" not in claude_agent.compose_system_prompt(
        "planner", "PROTOCOL BODY", None
    )


def test_format_inbound_lists_each_message() -> None:
    # format_inbound wraps each peer body in <untrusted-peer-data> fences (prompt-
    # injection defence); the attribution line sits OUTSIDE the fence so it cannot
    # be spoofed by message content.
    out = claude_agent.format_inbound(
        [
            {"sender": "a", "recipient": "all", "content": "hi"},
            {"sender": "b", "recipient": "planner", "content": "yo"},
        ]
    )
    assert "[caucus inbound]" in out
    # Attribution is outside the fence
    assert "from a (to all):" in out
    assert "from b (to planner):" in out
    # Content appears inside the fence
    assert "hi" in out
    assert "yo" in out
    # Fence markers are present — regression guard for the prompt-injection defence
    assert "<untrusted-peer-data>" in out
    assert "</untrusted-peer-data>" in out
    assert "say tool" in out


def test_agent_text_concatenates_text_blocks() -> None:
    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Msg:
        def __init__(self, content: list[Any]) -> None:
            self.content = content

    assert claude_agent._agent_text(_Msg([_Block("hello"), _Block("world")])) == "hello world"


def test_agent_text_ignores_non_text_messages() -> None:
    class _Result:
        pass

    assert claude_agent._agent_text(_Result()) is None


# --- loop control --------------------------------------------------------


class _FakeClient:
    """Records queries and interrupts; supports the async-context protocol.

    Stands in for :class:`ClaudeSDKClient`: it tracks the user turns driven into
    it, counts ``interrupt`` calls, and yields no response messages so a turn
    completes instantly.
    """

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.interrupts = 0

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for _ in ():  # empty async generator
            yield None

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _factory(*clients: _FakeClient) -> Any:
    """Return a client factory yielding each client in turn (one per lifecycle).

    A single client covers the no-reset cases; pass two to exercise the reset
    path, where the second client is built after the operator wipes the context.
    """
    pool = iter(clients)
    return lambda: next(pool)


class _FakeConnector:
    """Replays a scripted sequence of :class:`Inbound` batches, then stops.

    Records the ``ack_seq`` piggybacked on each poll in :attr:`acks`, so tests
    can assert the poller acknowledges the batch it just consumed.
    """

    def __init__(self, script: list[Inbound]) -> None:
        self._script = list(script)
        self.acks: list[int | None] = []

    async def receive(
        self, token: str, timeout: float, *, ack_seq: int | None = None
    ) -> Inbound:
        self.acks.append(ack_seq)
        if self._script:
            return self._script.pop(0)
        return Inbound(messages=[], mode="running", stop=True)


async def test_run_loop_injects_inbound_then_ends_on_stop() -> None:
    client = _FakeClient()
    connector = _FakeConnector(
        [Inbound([{"sender": "a", "recipient": "all", "content": "hi"}], "running", False)]
    )
    await claude_agent._run_loop(
        _factory(client), connector, "tok", poll_timeout=0.0, mission=None
    )
    assert len(client.queries) == 1
    assert "[caucus inbound]" in client.queries[0]
    assert "hi" in client.queries[0]


async def test_run_loop_mission_opens_the_exchange() -> None:
    client = _FakeClient()
    connector = _FakeConnector([])  # first poll returns the auto-stop
    await claude_agent._run_loop(
        _factory(client), connector, "tok", poll_timeout=0.0, mission="negotiate the API"
    )
    assert len(client.queries) == 1
    assert "[caucus mission]" in client.queries[0]
    assert "negotiate the API" in client.queries[0]


async def test_run_loop_stop_first_injects_nothing() -> None:
    client = _FakeClient()
    connector = _FakeConnector([Inbound([], "running", True)])
    await claude_agent._run_loop(
        _factory(client), connector, "tok", poll_timeout=0.0, mission=None
    )
    assert client.queries == []


async def test_run_loop_skips_quiet_polls() -> None:
    client = _FakeClient()
    connector = _FakeConnector(
        [
            Inbound([], "running", False),  # quiet
            Inbound([{"sender": "a", "recipient": "all", "content": "later"}], "running", False),
        ]
    )
    await claude_agent._run_loop(
        _factory(client), connector, "tok", poll_timeout=0.0, mission=None
    )
    assert len(client.queries) == 1
    assert "later" in client.queries[0]


# --- operator control: interrupt / reset --------------------------------


async def test_poll_inbound_interrupt_aborts_turn_without_ending() -> None:
    """An ``interrupt`` command calls interrupt() but neither stops nor resets."""
    client = _FakeClient()
    connector = _FakeConnector(
        [Inbound([], "running", False, commands=["interrupt"])]
    )
    stop, reset = asyncio.Event(), asyncio.Event()
    turns: asyncio.Queue[str] = asyncio.Queue()
    await claude_agent._poll_inbound(
        connector, "tok", client, turns, 0.0, stop, reset  # type: ignore[arg-type]
    )
    assert client.interrupts == 1
    assert stop.is_set()  # ends via the connector's auto-stop, not the interrupt
    assert not reset.is_set()


async def test_poll_inbound_reset_interrupts_and_signals_rebuild() -> None:
    """A ``reset`` command aborts the turn and sets the reset event, then returns."""
    client = _FakeClient()
    connector = _FakeConnector(
        [Inbound([], "running", False, commands=["reset"])]
    )
    stop, reset = asyncio.Event(), asyncio.Event()
    turns: asyncio.Queue[str] = asyncio.Queue()
    await claude_agent._poll_inbound(
        connector, "tok", client, turns, 0.0, stop, reset  # type: ignore[arg-type]
    )
    assert client.interrupts == 1
    assert reset.is_set()
    assert not stop.is_set()


async def test_run_loop_reset_rebuilds_client_with_fresh_context() -> None:
    """An operator reset tears down the first client and builds a second one."""
    first, second = _FakeClient(), _FakeClient()
    connector = _FakeConnector(
        [
            Inbound([{"sender": "a", "recipient": "all", "content": "hi"}], "running", False),
            Inbound([], "running", False, commands=["reset"]),
            Inbound([{"sender": "b", "recipient": "all", "content": "again"}], "running", False),
        ]
    )
    await claude_agent._run_loop(
        _factory(first, second), connector, "tok", poll_timeout=0.0, mission=None
    )
    # The reset aborted the first client and rebuilt onto the second, which is
    # the one that answers the post-reset traffic.
    assert first.interrupts == 1
    assert any("again" in q for q in second.queries)


# --- ACK piggyback -------------------------------------------------------


async def test_poll_inbound_acks_the_previous_batch_on_the_next_poll() -> None:
    """Each poll piggybacks the highest seq of the batch the previous one gave.

    Without this the hub's unacked ring buffer never drains and a reap+revive
    replays the whole backlog as fresh inbound.
    """
    client = _FakeClient()
    connector = _FakeConnector(
        [
            Inbound(
                [
                    {"sender": "a", "recipient": "all", "content": "one", "seq": 7},
                    {"sender": "a", "recipient": "all", "content": "two", "seq": 9},
                ],
                "running",
                False,
            ),
            Inbound([], "running", False),  # quiet: nothing new to acknowledge
        ]
    )
    stop, reset = asyncio.Event(), asyncio.Event()
    turns: asyncio.Queue[str] = asyncio.Queue()
    await claude_agent._poll_inbound(
        connector, "tok", client, turns, 0.0, stop, reset  # type: ignore[arg-type]
    )
    # First poll has nothing to ack; the second carries the batch's highest seq;
    # the third (after a quiet poll) carries nothing again.
    assert connector.acks[:3] == [None, 9, None]


async def test_poll_inbound_retries_an_unsent_ack_after_a_hub_error() -> None:
    """A failed poll must not swallow the ACK it was carrying."""

    class _FlakyConnector(_FakeConnector):
        """Raises once on the poll that would have carried the first ACK."""

        def __init__(self, script: list[Inbound]) -> None:
            super().__init__(script)
            self._boom = True

        async def receive(
            self, token: str, timeout: float, *, ack_seq: int | None = None
        ) -> Inbound:
            if ack_seq is not None and self._boom:
                self._boom = False
                self.acks.append(ack_seq)
                raise httpx.ConnectError("hub went away")
            return await super().receive(token, timeout, ack_seq=ack_seq)

    client = _FakeClient()
    connector = _FlakyConnector(
        [Inbound([{"sender": "a", "recipient": "all", "content": "hi", "seq": 4}], "running", False)]
    )
    stop, reset = asyncio.Event(), asyncio.Event()
    turns: asyncio.Queue[str] = asyncio.Queue()
    # The backoff floor is 1s; shrink it so the retry is immediate.
    original = claude_agent._BACKOFF_MIN
    claude_agent._BACKOFF_MIN = 0.0
    try:
        await claude_agent._poll_inbound(
            connector, "tok", client, turns, 0.0, stop, reset  # type: ignore[arg-type]
        )
    finally:
        claude_agent._BACKOFF_MIN = original
    # The ACK the failed poll was carrying is re-sent on the retry, not lost.
    assert connector.acks.count(4) == 2


async def test_poll_inbound_acks_drain_the_hub_replay_buffer(live_hub: str) -> None:
    """End to end: the hub's unacked buffer drains as the poller acknowledges.

    Runs the real poller against a real hub, so the regression is pinned on the
    hub-side bookkeeping (``last_acked_seq`` / ``unacked``) rather than on the
    connector call alone.
    """
    async with HubConnector(live_hub) as hub:
        me = await hub.register("ack-drain-rx", None)
        peer = await hub.register("ack-drain-tx", None)
        await hub.send(peer.token, "ack-drain-rx", "please ack me")

        client = _FakeClient()
        stop, reset = asyncio.Event(), asyncio.Event()
        turns: asyncio.Queue[str] = asyncio.Queue()
        poller = asyncio.ensure_future(
            claude_agent._poll_inbound(
                hub, me.token, client, turns, 1.0, stop, reset  # type: ignore[arg-type]
            )
        )
        try:
            hub_client = hub_module.state.client_for(me.token)
            assert hub_client is not None
            deadline = asyncio.get_event_loop().time() + 10.0
            while (
                hub_client.last_acked_seq == 0
                and asyncio.get_event_loop().time() < deadline
            ):
                await asyncio.sleep(0.05)
        finally:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)

    assert hub_client.last_acked_seq > 0
    assert not [m for m in hub_client.unacked if m.seq > hub_client.last_acked_seq]


async def test_safe_interrupt_swallows_errors() -> None:
    """A client whose interrupt() raises does not blow up the poller."""

    class _Boom:
        async def interrupt(self) -> None:
            raise RuntimeError("no turn in flight")

    await claude_agent._safe_interrupt(_Boom())  # type: ignore[arg-type]


# --- NameInUseError → clean exit -----------------------------------------


async def test_run_session_exits_cleanly_on_name_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_session returns without raising when register raises NameInUseError.

    Stubs the connector so register() raises immediately, verifying the
    except-NameInUseError handler swallows the error into a clean return.
    """
    from caucus.hub_connector import NameInUseError

    class _FakeProtocol:
        version = 8
        text = "PROTOCOL"

    class _FakeConnector:
        async def fetch_protocol(self) -> _FakeProtocol:
            return _FakeProtocol()

        async def register(self, project: str, version: int, token: str | None = None) -> None:
            raise NameInUseError("already taken")

        async def __aenter__(self) -> _FakeConnector:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

    monkeypatch.setattr(claude_agent, "HubConnector", lambda *a, **kw: _FakeConnector())

    # Must return without raising — NameInUseError is swallowed into a clean exit.
    await claude_agent.run_session(
        hub_url="http://unused",
        project="alpha",
        mission=None,
        model=None,
        poll_timeout=0.0,
    )


# --- option wiring (type + permission mode → ClaudeAgentOptions) ---------


class _FakeMe:
    project = "alpha"
    protocol_version = 8
    note = None
    channels: dict[str, Any] = {}
    token = "tok"


class _RegisteringConnector:
    """Connector stub that registers cleanly so run_session reaches options build."""

    async def fetch_protocol(self) -> Any:
        class _P:
            version = 8
            text = "PROTOCOL"

        return _P()

    async def register(self, project: str, version: int, token: str | None = None) -> _FakeMe:
        return _FakeMe()

    async def leave(self, token: str) -> None:
        return None

    async def __aenter__(self) -> _RegisteringConnector:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


def _capture_options(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Wire run_session to a fake SDK client and capture the options it builds."""
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, options: Any) -> None:
            captured["options"] = options

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

    async def _noop_loop(client_factory: Any, *args: Any, **kwargs: Any) -> None:
        # run_session now defers client construction to a factory (so an operator
        # reset can rebuild the client on a clean context). Build one here so the
        # options it carries are captured, then return without listening.
        client_factory()
        return None

    monkeypatch.setattr(claude_agent, "HubConnector", lambda *a, **kw: _RegisteringConnector())
    monkeypatch.setattr(claude_agent, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(claude_agent, "_run_loop", _noop_loop)
    return captured


async def test_run_session_defaults_to_talker_with_auto_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default session is a talker gated by the 'auto' permission mode."""
    captured = _capture_options(monkeypatch)
    await claude_agent.run_session(
        hub_url="http://unused",
        project="alpha",
        mission=None,
        model=None,
        poll_timeout=0.0,
    )
    opts = captured["options"]
    assert opts.permission_mode == "auto"
    assert "Bash" in opts.disallowed_tools
    assert "mcp__caucus__say" in opts.allowed_tools


async def test_run_session_worker_gets_builtins_and_chosen_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker session grants the built-ins and honours an explicit mode."""
    captured = _capture_options(monkeypatch)
    await claude_agent.run_session(
        hub_url="http://unused",
        project="alpha",
        mission=None,
        model=None,
        poll_timeout=0.0,
        agent_type="worker",
        permission_mode="bypassPermissions",
    )
    opts = captured["options"]
    assert opts.permission_mode == "bypassPermissions"
    assert opts.disallowed_tools == []
    assert "Bash" in opts.allowed_tools
    assert "mcp__caucus__say" in opts.allowed_tools
