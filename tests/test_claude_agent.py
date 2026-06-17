"""Unit tests for the native Claude connector's pure logic and loop control.

The SDK-bound pieces (``ClaudeSDKClient``, the in-process tools) are integration
surface; here we test the parts that carry the behaviour and need no live model:
prompt composition, inbound formatting, assistant-text extraction, and the
listen → inject → reply control flow driven against lightweight fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from caucus import claude_agent
from caucus.hub_connector import Inbound


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
    """Records queries and yields no response messages."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for _ in ():  # empty async generator
            yield None


class _FakeConnector:
    """Replays a scripted sequence of :class:`Inbound` batches, then stops."""

    def __init__(self, script: list[Inbound]) -> None:
        self._script = list(script)

    async def receive(self, token: str, timeout: float) -> Inbound:
        if self._script:
            return self._script.pop(0)
        return Inbound(messages=[], mode="running", stop=True)


async def test_run_loop_injects_inbound_then_ends_on_stop() -> None:
    client = _FakeClient()
    connector = _FakeConnector(
        [Inbound([{"sender": "a", "recipient": "all", "content": "hi"}], "running", False)]
    )
    await claude_agent._run_loop(
        client, connector, "tok", poll_timeout=0.0, mission=None  # type: ignore[arg-type]
    )
    assert len(client.queries) == 1
    assert "[caucus inbound]" in client.queries[0]
    assert "hi" in client.queries[0]


async def test_run_loop_mission_opens_the_exchange() -> None:
    client = _FakeClient()
    connector = _FakeConnector([])  # first poll returns the auto-stop
    await claude_agent._run_loop(
        client, connector, "tok", poll_timeout=0.0, mission="negotiate the API"  # type: ignore[arg-type]
    )
    assert len(client.queries) == 1
    assert "[caucus mission]" in client.queries[0]
    assert "negotiate the API" in client.queries[0]


async def test_run_loop_stop_first_injects_nothing() -> None:
    client = _FakeClient()
    connector = _FakeConnector([Inbound([], "running", True)])
    await claude_agent._run_loop(
        client, connector, "tok", poll_timeout=0.0, mission=None  # type: ignore[arg-type]
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
        client, connector, "tok", poll_timeout=0.0, mission=None  # type: ignore[arg-type]
    )
    assert len(client.queries) == 1
    assert "later" in client.queries[0]


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

    async def _noop_loop(*args: Any, **kwargs: Any) -> None:
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
