"""Unit tests for the in-process Streamable HTTP MCP server (:mod:`caucus.mcp_http`).

These drive the tool callables directly with a synthetic ``Context`` carrying a
fixed ``Mcp-Session-Id`` header, against the hub's real app over an in-process
ASGI transport and a fresh :class:`HubState` (the ``state`` fixture). That keeps
the focus on the parts unique to this module: per-session membership keyed on the
session id, the ``join`` response shaping that replicates the ``/register``
handler (A1), the A3 fail-closed gate, the watcher command, the provable mcp
floor (A7), and the bridge/http tool-surface drift guard (A2).

End-to-end coverage over a genuine Streamable HTTP handshake (gating parity,
origin rejection, lifecycle) lives in :mod:`tests.test_mcp_http_integration`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from caucus import hub as hub_module
from caucus import mcp_bridge
from caucus.hub_connector import HubConnector
from caucus.mcp_http import build_mcp_server
from caucus.state import CapExceeded, HubState

_SELF_URL = "http://127.0.0.1:8765"


def _ctx(session_id: str | None, *, client_name: str | None = None) -> Any:
    """Build a minimal stand-in for a FastMCP ``Context`` carrying a session id.

    The tools read ``ctx.request_context.request.headers`` and, for the default
    identity only, ``ctx.session.client_params.clientInfo.name``; this fake
    supplies exactly those. ``session_id=None`` yields a request whose headers
    lack the key, exercising the A3 fail-closed path. ``client_name=None`` omits
    ``session`` entirely, exercising the ``mcp-client`` fallback for a handshake
    that carried no usable client identity.
    """
    headers = {"mcp-session-id": session_id} if session_id is not None else {}
    request = type("_Req", (), {"headers": headers})()
    request_context = type("_RC", (), {"request": request})()
    attrs: dict[str, Any] = {"request_context": request_context}
    if client_name is not None:
        info = type("_Info", (), {"name": client_name})()
        params = type("_Params", (), {"clientInfo": info})()
        attrs["session"] = type("_Session", (), {"client_params": params})()
    return type("_Ctx", (), attrs)()


def _tool(server: Any, name: str) -> Any:
    """Return the registered tool's underlying callable for a direct call."""
    return server._tool_manager.get_tool(name).fn


def _build() -> Any:
    """Build a fresh in-process MCP server bound to the hub app."""
    return build_mcp_server(hub_module.app, self_url=_SELF_URL)


async def test_join_arms_session_and_registers(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    # No setup step: join arms the session (fetches the protocol) then registers.
    join_res = await _tool(server, "join")(ctx, project="alpha")
    assert join_res["joined"] is True
    assert join_res["project"] == "alpha"
    assert join_res["hub"] == _SELF_URL
    assert "Caucus operating protocol" in join_res["protocol"]
    assert "alpha" in state.peers()


async def test_sessions_are_isolated(state: HubState) -> None:
    server = _build()
    s1, s2 = _ctx("s1"), _ctx("s2")
    await _tool(server, "join")(s1, project="alpha")
    await _tool(server, "join")(s2, project="beta")

    who1 = await _tool(server, "whoami")(s1)
    who2 = await _tool(server, "whoami")(s2)
    assert who1["joined_as"] == "alpha"
    assert who2["joined_as"] == "beta"

    # Leaving one session must not disturb the other's membership or roster.
    await _tool(server, "leave")(s1)
    assert (await _tool(server, "whoami")(s1))["joined"] is False
    assert (await _tool(server, "whoami")(s2))["joined"] is True
    assert "alpha" not in state.peers()
    assert "beta" in state.peers()


async def test_channel_tool_after_a_kick_reports_session_expired(
    state: HubState,
) -> None:
    """The ``/mcp`` path must name a dead session, not blame the channel name.

    ``HubConnector`` folded 401, 403 and 429 into one ``False``, so a kicked
    peer was told ``channel_rejected`` with a hint about the ``#`` prefix, and
    every retry with a "better" name failed the same way.
    """
    server = _build()
    ctx = _ctx("s-kicked")
    await _tool(server, "join")(ctx, project="gamma")
    await _tool(server, "join_channel")(ctx, channel="#gamma-room")
    assert state.kick("gamma") is True

    for tool_name, kwargs in (
        ("join_channel", {"channel": "#gamma-room"}),
        ("leave_channel", {"channel": "#gamma-room"}),
        ("set_channel_topic", {"channel": "#gamma-room", "topic": "t"}),
    ):
        res = await _tool(server, tool_name)(ctx, **kwargs)
        assert res["error"] == "session_expired", tool_name
        assert res["joined_as"] == "gamma"
        assert "join()" in str(res["hint"])


async def test_any_tool_after_a_kick_reports_session_expired(state: HubState) -> None:
    """Not just the channel tools: every token-bearing ``/mcp`` tool must say it.

    A kicked session used to read ``hub_unreachable`` from ``say`` and ``peek``,
    then ``not_joined`` once the next reaper sweep cleared its record, and never
    learned that its membership was what had gone.
    """
    server = _build()
    ctx = _ctx("s-kicked-any")
    await _tool(server, "join")(ctx, project="epsilon")
    assert state.kick("epsilon") is True

    said = await _tool(server, "say")(ctx, content="still here?")
    assert said["error"] == "session_expired"
    assert said["joined_as"] == "epsilon"
    assert "join()" in str(said["hint"])

    peeked = await _tool(server, "peek")(ctx)
    assert peeked["error"] == "session_expired"
    assert peeked["joined_as"] == "epsilon"


async def test_unauthenticated_tool_401_is_not_called_a_session_expiry(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 the session's token never earned keeps the hub_unreachable contract.

    Stands in for an auth proxy in front of the hub: ``list_peers`` carries no
    token, so a joined session must not read its rejection as a lost membership
    and go re-joining a hub that never let it through.
    """
    server = _build()
    ctx = _ctx("s-proxy")
    await _tool(server, "join")(ctx, project="zeta")

    async def _unauthorized(self: HubConnector) -> list[str]:
        request = httpx.Request("GET", "http://proxy.invalid/peers")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(HubConnector, "peers", _unauthorized)
    result = await _tool(server, "list_peers")(ctx)
    assert result["error"] == "hub_unreachable"


async def test_channel_tool_keeps_channel_rejected_for_a_bad_name(
    state: HubState,
) -> None:
    """A genuine rejection keeps its own code, so the two stay distinguishable."""
    server = _build()
    ctx = _ctx("s-badname")
    await _tool(server, "join")(ctx, project="delta")
    res = await _tool(server, "join_channel")(ctx, channel="no-hash-prefix")
    assert res["error"] == "channel_rejected"
    assert "#" in str(res["hint"])


async def test_set_channel_topic_non_member_is_topic_rejected(
    state: HubState,
) -> None:
    """A live session refused for non-membership is not a session expiry."""
    server = _build()
    owner, outsider = _ctx("s-owner"), _ctx("s-outsider")
    await _tool(server, "join")(owner, project="owner")
    await _tool(server, "join_channel")(owner, channel="#owned")
    await _tool(server, "join")(outsider, project="outsider")
    res = await _tool(server, "set_channel_topic")(
        outsider, channel="#owned", topic="nope"
    )
    assert res["error"] == "topic_rejected"


async def test_tools_fail_closed_without_session_id(state: HubState) -> None:
    """A3: with no Mcp-Session-Id, every gated tool fails closed to no_session."""
    server = _build()
    ctx = _ctx(None)
    assert (await _tool(server, "list_peers")(ctx))["error"] == "no_session"
    assert (await _tool(server, "join")(ctx))["error"] == "no_session"


async def test_arming_failure_returns_hub_unreachable(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub blip on the first tool call is refused by the arming gate, not raised.

    ``_ensure_armed`` fetches the protocol before any tool body runs; when that
    fetch dies it must return the structured ``hub_unreachable`` contract. Only
    ``fetch_protocol`` is broken here (``/peers`` still answers), so a pass would
    prove the *gate* refused, not merely that the tool body blew up.
    """
    server = _build()
    ctx = _ctx("s1")

    async def _boom(_self: Any) -> None:
        raise httpx.ConnectError("hub down")

    monkeypatch.setattr(HubConnector, "fetch_protocol", _boom)
    res = await _tool(server, "list_peers")(ctx)
    assert res["error"] == "hub_unreachable"
    assert res["hub"] == _SELF_URL
    assert "hub down" in str(res["detail"])
    # The gate bailed before sessions.setdefault(): no half-armed record is left
    # behind for the retry to inherit.
    assert (await _tool(server, "whoami")(ctx))["armed"] is False

    # Not terminal: once the hub answers again the same session arms and works.
    monkeypatch.undo()
    assert "peers" in await _tool(server, "list_peers")(ctx)
    assert (await _tool(server, "whoami")(ctx))["armed"] is True


async def test_readonly_tools_arm_and_work_before_join(state: HubState) -> None:
    """Scout-before-join parity with the stdio bridge (see test_bridge.py's
    ``test_readonly_tools_work_before_join``): every read-only tool succeeds
    without ever calling ``join``, arming the session as a side effect only.
    """
    server = _build()
    ctx = _ctx("s1")  # known session id, never joined
    assert "peers" in await _tool(server, "list_peers")(ctx)
    assert (await _tool(server, "whoami")(ctx))["armed"] is True
    assert (await _tool(server, "ping")(ctx, peer="nobody"))["state"] == "absent"
    assert "channels" in await _tool(server, "list_channels")(ctx)
    assert "forms" in await _tool(server, "list_forms")(ctx)
    assert "floors" in await _tool(server, "floor")(ctx, action="status")
    section = await _tool(server, "protocol_section")(ctx, name="channels")
    assert section["section"] == "channels"


async def test_protocol_section_unknown_name_returns_the_real_list(
    state: HubState,
) -> None:
    """A caller's typo comes back as a readable dict over this transport too.

    ``HubConnector.fetch_protocol_section`` has to short-circuit the hub's 404
    before ``raise_for_status``, or ``_resilient`` reports a hub outage for what
    is really a bad section name.
    """
    server = _build()
    res = await _tool(server, "protocol_section")(_ctx("s1"), name="nope")
    assert res["error"] == "unknown_section"
    assert "talking-stick" in res["sections"]


async def test_say_requires_join(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    res = await _tool(server, "say")(ctx, content="hi")
    assert res["error"] == "not_joined"


async def test_peek_requires_join(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    res = await _tool(server, "peek")(ctx)
    assert res["error"] == "not_joined"


async def test_peek_reports_pending_without_draining(state: HubState) -> None:
    server = _build()
    sender_ctx, receiver_ctx = _ctx("sender"), _ctx("receiver")
    await _tool(server, "join")(sender_ctx, project="peek-tx")
    await _tool(server, "join")(receiver_ctx, project="peek-rx")

    await _tool(server, "say")(sender_ctx, content="hi there", to="peek-rx")

    before = await _tool(server, "peek")(receiver_ctx)
    assert before["pending"] == 1
    assert before["last"] == {"sender": "peek-tx", "preview": "hi there"}

    # A peek never drains: listen() still sees the message afterwards.
    got = await _tool(server, "listen")(receiver_ctx, timeout=3)
    assert any("hi there" in m["content"] for m in got["messages"])

    after = await _tool(server, "peek")(receiver_ctx)
    assert after == {"pending": 0, "last": None}


async def test_decisions_requires_join(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    res = await _tool(server, "decisions")(ctx)
    assert res["error"] == "not_joined"


async def test_decisions_lists_settled_form(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="form-decider")
    form_id = (
        await _tool(server, "ask_operator")(
            ctx,
            title="Deploy?",
            fields=[
                {
                    "key": "ok",
                    "label": "Proceed?",
                    "type": "radio",
                    "options": ["yes", "no"],
                }
            ],
            to="all",
        )
    )["form_id"]
    state.answer_form(form_id, {"ok": "yes"})

    entries = (await _tool(server, "decisions")(ctx))["decisions"]
    assert any(
        e["title"] == "Deploy?" and e["asker"] == "form-decider" for e in entries
    )


async def test_decisions_channel_scoped_answer_invisible_to_non_member(
    state: HubState,
) -> None:
    server = _build()
    asker_ctx, outsider_ctx = _ctx("asker"), _ctx("outsider")
    await _tool(server, "join")(asker_ctx, project="channel-decider")
    await _tool(server, "join")(outsider_ctx, project="channel-outsider")
    form_id = (
        await _tool(server, "ask_operator")(
            asker_ctx,
            title="Rotate the key?",
            fields=[
                {
                    "key": "ok",
                    "label": "Proceed?",
                    "type": "radio",
                    "options": ["yes", "no"],
                }
            ],
            to="#secret",
        )
    )["form_id"]
    state.answer_form(form_id, {"ok": "yes"})

    # The asker auto-subscribed to #secret when it opened the form.
    own = (await _tool(server, "decisions")(asker_ctx))["decisions"]
    assert any(e["title"] == "Rotate the key?" for e in own)

    outsider = (await _tool(server, "decisions")(outsider_ctx))["decisions"]
    assert outsider == []


async def test_join_name_in_use_on_contested(state: HubState) -> None:
    """A1: a live listener holding the name yields name_in_use, no crash.

    CONTESTED returns ``Registration(client=None)``; the shaping must not
    dereference ``reg.client``. Seed a live listener (``active_polls > 0``) under
    the name, then join from a fresh session with no matching token.
    """
    reg = state.register("alpha")
    assert reg.client is not None
    reg.client.active_polls = 1  # simulate an in-flight /receive long-poll

    server = _build()
    ctx = _ctx("s1")
    res = await _tool(server, "join")(ctx, project="alpha")
    assert res["error"] == "name_in_use"
    assert res["project"] == "alpha"
    assert "note" in res


async def test_join_replaced_includes_note(state: HubState) -> None:
    """A1: taking over a dead slot (REPLACED) carries the advisory note."""
    state.register("alpha")  # active_polls stays 0 -> a dead/timed-out slot

    server = _build()
    ctx = _ctx("s1")
    res = await _tool(server, "join")(ctx, project="alpha")
    assert res["joined"] is True
    assert "mid-conversation" in res["note"]


async def test_two_default_joins_do_not_merge_into_one_identity(
    state: HubState,
) -> None:
    """Two sessions joining without a project must not collapse onto one peer.

    The regression: the default name was a process-wide constant, so both
    sessions asked for the same name. The first holds no in-flight ``/receive``
    poll, so the hub saw ``active_polls == 0``, returned REPLACED instead of
    CONTESTED, and handed the second session the *existing* Client record:
    same token, same inbox. Two agents, one identity, no error anywhere.
    """
    server = _build()
    a, b = _ctx("session-A"), _ctx("session-B")

    first = await _tool(server, "join")(a)
    assert first["joined"] is True

    second = await _tool(server, "join")(b)
    assert second["error"] == "name_in_use"
    assert second["project"] == first["project"]

    # The incumbent keeps its membership; the newcomer holds none.
    assert (await _tool(server, "whoami")(a))["joined"] is True
    assert (await _tool(server, "whoami")(b))["joined"] is False
    assert state.peers() == [first["project"]]


async def test_live_session_collision_refused_for_explicit_name(
    state: HubState,
) -> None:
    """The same guard holds when both sessions name themselves explicitly.

    Independent of how the default is derived: the hub cannot see that a peer
    with no in-flight poll is alive, so the refusal has to come from this
    process, which knows both sessions exist.
    """
    server = _build()
    a, b = _ctx("session-A"), _ctx("session-B")
    assert (await _tool(server, "join")(a, project="alpha"))["joined"] is True

    res = await _tool(server, "join")(b, project="alpha")
    assert res["error"] == "name_in_use"
    assert "explicit" in res["note"]
    assert state.peers() == ["alpha"]


async def test_rejoining_same_session_is_not_a_collision(state: HubState) -> None:
    """The live-session guard must not fire on a session re-joining itself.

    ``join`` is documented idempotent (REAFFIRMED via the cached token); a
    self-match in the session table would turn that into a spurious refusal.
    """
    server = _build()
    ctx = _ctx("session-A")
    assert (await _tool(server, "join")(ctx, project="alpha"))["joined"] is True
    again = await _tool(server, "join")(ctx, project="alpha")
    assert again["joined"] is True
    assert state.peers() == ["alpha"]


async def test_left_session_frees_the_name(state: HubState) -> None:
    """A session that left releases its name for another session to take."""
    server = _build()
    a, b = _ctx("session-A"), _ctx("session-B")
    await _tool(server, "join")(a, project="alpha")
    await _tool(server, "leave")(a)
    assert (await _tool(server, "join")(b, project="alpha"))["joined"] is True
    assert state.peers() == ["alpha"]


@pytest.mark.parametrize("name", ["human", "hub", "system", "HUMAN", " Hub "])
async def test_join_refuses_reserved_names(state: HubState, name: str) -> None:
    """A1 gap: the direct-register shortcut skipped RegisterRequest's guards.

    ``POST /register`` answers 422 for these names because registering as
    ``human`` or ``hub`` lets a peer fabricate operator or control-plane
    authority in the ``sender`` field other agents read. The ``/mcp`` join path
    bypasses that pydantic model, so it must refuse them itself.
    """
    server = _build()
    res = await _tool(server, "join")(_ctx("s1"), project=name)
    assert res["error"] == "reserved_name"
    assert state.peers() == []


@pytest.mark.parametrize("name", ["", "x" * 65])
async def test_join_refuses_out_of_bounds_names(state: HubState, name: str) -> None:
    """The 1-64 character bound from ``RegisterRequest.project`` is re-applied."""
    server = _build()
    res = await _tool(server, "join")(_ctx("s1"), project=name)
    assert res["error"] == "invalid_name"
    assert state.peers() == []


async def test_default_project_comes_from_client_info(state: HubState) -> None:
    """The default identity is per-session, taken from the MCP handshake."""
    server = _build()
    a = _ctx("session-A", client_name="codex")
    b = _ctx("session-B", client_name="gemini")

    assert (await _tool(server, "whoami")(a))["default_project"] == "codex"
    assert (await _tool(server, "join")(a))["project"] == "codex"
    assert (await _tool(server, "join")(b))["project"] == "gemini"
    assert sorted(state.peers()) == ["codex", "gemini"]


@pytest.mark.parametrize(
    ("client_name", "expected"),
    [
        ("claude code", "claude-code"),  # whitespace collapsed, never raw
        ("  spaced  ", "spaced"),  # surrounding dashes trimmed after collapse
        ("bad\nname\x00", "bad-name"),  # control characters cannot reach the roster
        ("human", "mcp-client"),  # a reserved clientInfo falls back, never wins
        ("!!!", "mcp-client"),  # nothing usable survives cleaning
        ("z" * 100, "z" * 64),  # truncated to the registrable bound
    ],
)
async def test_client_info_name_is_sanitized(
    state: HubState, client_name: str, expected: str
) -> None:
    """``clientInfo.name`` is client-controlled, so it is cleaned before use.

    It lands in the roster and the operator console, and the reserved names are
    exactly the ones a hostile client would pick.
    """
    server = _build()
    res = await _tool(server, "join")(_ctx("s1", client_name=client_name))
    assert res["project"] == expected
    assert state.peers() == [expected]


async def test_join_cap_exceeded(state: HubState, monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: a CapExceeded from register is surfaced as a clean error, not a crash."""
    server = _build()
    ctx = _ctx("s1")

    def _boom(*_a: object, **_k: object) -> None:
        raise CapExceeded("client limit reached")

    monkeypatch.setattr(state, "register", _boom)
    res = await _tool(server, "join")(ctx, project="alpha")
    assert res["error"] == "cap_exceeded"


async def test_join_protocol_stale(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: a session behind the protocol revision gets stale + fresh text."""
    server = _build()
    ctx = _ctx("s1")
    # Arm at the current revision via a read-only tool, then bump the hub's
    # protocol past it so the subsequent join sees the session as behind.
    await _tool(server, "list_peers")(ctx)
    monkeypatch.setattr(
        hub_module, "PROTOCOL_VERSION", hub_module.PROTOCOL_VERSION + 1
    )
    res = await _tool(server, "join")(ctx, project="alpha")
    assert res["protocol_stale"] is True
    assert "protocol" in res
    assert str(res["note"]).startswith("protocol updated")


async def test_second_join_omits_the_protocol_text(state: HubState) -> None:
    """The ~4.4k-token manual is delivered once per session, not per join."""
    server = _build()
    ctx = _ctx("s1")
    first = await _tool(server, "join")(ctx, project="alpha")
    assert "Caucus operating protocol" in first["protocol"]

    second = await _tool(server, "join")(ctx, project="alpha")
    assert "protocol" not in second
    assert second["protocol_stale"] is False
    assert "already delivered this session" in str(second["note"])
    # The note must name the way out, or it is a dead end for an agent whose
    # context was compacted and no longer holds the manual.
    assert "force_protocol=true" in str(second["note"])


async def test_force_protocol_redelivers_the_text(state: HubState) -> None:
    """force_protocol is the post-compaction escape hatch: it re-sends the text."""
    server = _build()
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")
    forced = await _tool(server, "join")(ctx, project="alpha", force_protocol=True)
    assert "Caucus operating protocol" in forced["protocol"]
    assert "already delivered" not in str(forced.get("note", ""))


async def test_stale_protocol_redelivers_without_force(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revision bump still pushes the new text on the next join, unasked."""
    server = _build()
    ctx = _ctx("s1")
    assert "protocol" in await _tool(server, "join")(ctx, project="alpha")

    monkeypatch.setattr(
        hub_module, "PROTOCOL_VERSION", hub_module.PROTOCOL_VERSION + 1
    )
    second = await _tool(server, "join")(ctx, project="alpha")
    assert second["protocol_stale"] is True
    assert "protocol" in second
    assert "re-read the protocol" in str(second["note"])


async def test_protocol_delivery_is_per_session(state: HubState) -> None:
    """One session having read the manual must not deprive the next one of it."""
    server = _build()
    await _tool(server, "join")(_ctx("s1"), project="alpha")
    fresh = await _tool(server, "join")(_ctx("s2"), project="beta")
    assert "Caucus operating protocol" in fresh["protocol"]


async def test_watch_command_uses_self_url_and_token_file(state: HubState) -> None:
    """watch_command points at the real hub URL and writes a 0600 token file."""
    import os

    server = build_mcp_server(hub_module.app, self_url="http://127.0.0.1:9999")
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")

    res = await _tool(server, "watch_command")(ctx)
    command = str(res["command"])
    assert command.startswith("caucus-watch --hub http://127.0.0.1:9999 --token-file ")
    path = command.split("--token-file ", 1)[1]
    assert os.path.exists(path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    # leave() removes the watcher token file.
    await _tool(server, "leave")(ctx)
    assert not os.path.exists(path)


# --- floor: per-action dispatch (mirrors tests/test_bridge.py's talking-stick
# coverage) so a future raise<->pass swap or a wrong drop/pass fall-through in
# the HTTP floor() dispatch (mcp_http.py) is caught, not just proxied silently.


async def test_floor_action_take(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alice")
    res = await _tool(server, "floor")(
        ctx, action="take", scope="all", reason="prod is down"
    )
    assert res == {
        "ok": True,
        "scope": "all",
        "holder": "alice",
        "reason": "prod is down",
    }


async def test_floor_action_raise(state: HubState) -> None:
    server = _build()
    holder, waiter = _ctx("holder"), _ctx("waiter")
    await _tool(server, "join")(holder, project="alice")
    await _tool(server, "join")(waiter, project="bob")
    await _tool(server, "floor")(holder, action="take", reason="x")
    res = await _tool(server, "floor")(waiter, action="raise")
    assert res == {"ok": True, "scope": "all", "position": 1}


async def test_floor_action_pass(state: HubState) -> None:
    server = _build()
    holder, waiter = _ctx("holder"), _ctx("waiter")
    await _tool(server, "join")(holder, project="alice")
    await _tool(server, "join")(waiter, project="bob")
    await _tool(server, "floor")(holder, action="take", reason="x")
    await _tool(server, "floor")(waiter, action="raise")
    res = await _tool(server, "floor")(holder, action="pass")
    assert res["passed_to"] == "bob"
    # The stick genuinely moved: the old holder is now barred, the new one
    # is not — this is what would break if raise/pass were swapped.
    assert (await _tool(server, "floor")(waiter, action="pass")).get("released") is True


async def test_floor_action_drop(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alice")
    await _tool(server, "floor")(ctx, action="take", reason="x")
    res = await _tool(server, "floor")(ctx, action="drop")
    assert res["released"] is True
    # Dropped, not merely passed: status shows the lane fully reopened.
    assert (await _tool(server, "floor")(ctx, action="status"))["floors"] == {}


async def test_floor_action_status_works_before_join(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")  # armed only, never joined
    assert (await _tool(server, "floor")(ctx, action="status")) == {"floors": {}}


async def test_floor_take_requires_join(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")  # armed only, never joined
    res = await _tool(server, "floor")(ctx, action="take", reason="x")
    assert res == {"error": "not_joined", "hint": "call join() first"}


async def test_floor_rejects_unknown_action(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alice")
    res = await _tool(server, "floor")(ctx, action="wiggle")
    assert res["error"] == "invalid_action"


def test_mcp_floor_exposes_streamable_and_security() -> None:
    """A7: the installed mcp floor exposes both required capabilities."""
    from mcp.server.fastmcp import FastMCP

    probe = FastMCP("probe")
    assert hasattr(probe, "streamable_http_app")
    assert hasattr(probe.settings, "transport_security")


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        # Loopback on ANY port is allowed by default (the Inspector case).
        ("http://127.0.0.1:8765", True),
        ("http://127.0.0.1:6274", True),
        ("http://localhost:6274", True),
        ("http://[::1]:9999", True),
        # An operator-approved extra origin matches only itself.
        ("https://console.example.com", True),
        ("https://console.example.com:1", False),
        # A cross-site origin is refused.
        ("http://evil.example.com", False),
        ("http://127.0.0.1.evil.com:6274", False),
    ],
)
def test_origin_allowed_matches_loopback_and_extras(
    origin: str, expected: bool
) -> None:
    """The CORS/transport allowlist accepts loopback (any port) plus extras.

    Guards the shared allowlist the CORS layer and the transport both read, so
    the ``:*`` port wildcard and the exact-match extras behave as documented.
    """
    from caucus.mcp_http import effective_allowed_origins, origin_allowed

    patterns = effective_allowed_origins(["https://console.example.com"])
    assert origin_allowed(origin, patterns) is expected


def _schema_signature(schema: dict[str, Any]) -> tuple[Any, Any]:
    """Normalize an MCP input schema to (properties-without-title, required)."""
    props = {
        name: {k: v for k, v in spec.items() if k != "title"}
        for name, spec in schema.get("properties", {}).items()
    }
    return props, frozenset(schema.get("required", []))


# Tools whose descriptions are known-good, *verified* divergences rather than
# accidental drift: each was checked against the actual runtime behaviour of
# both connectors (see the ``feat/slim-tool-surface`` history) and genuinely
# differs because the HTTP-mounted server drives these calls through
# HubConnector methods with a coarser error surface than the stdio bridge's
# direct status-code handling (``join``/``join_channel``/``leave_channel``/
# ``set_channel_topic``), or because each connector correctly names its own
# component (``listen``'s "connector" vs "bridge" tracks the seq). ``join`` and
# ``watch_command`` diverge for a third reason: only the stdio bridge folds the
# ``caucus-watch`` command into its ``join`` result, because only a passive host
# needs the out-of-band watcher at all. Any other tool drifting out of the two
# lists below is a genuine regression.
_KNOWN_DESCRIPTION_DIVERGENCE = {
    "join",
    "join_channel",
    "leave_channel",
    "set_channel_topic",
    "listen",
    "watch_command",
}


def _normalize_description(text: str) -> str:
    """Collapse whitespace so cosmetic docstring re-wrapping cannot trip the guard."""
    return " ".join(text.split())


async def test_tool_surface_matches_bridge(state: HubState) -> None:
    """A2 drift guard: identical tool names, descriptions, and input arg schemas vs the bridge."""
    server = _build()
    http_tools = {t.name: t for t in await server.list_tools()}
    bridge_tools = {t.name: t for t in await mcp_bridge.mcp.list_tools()}

    assert set(http_tools) == set(bridge_tools)
    for name in http_tools:
        http_sig = _schema_signature(http_tools[name].inputSchema)
        bridge_sig = _schema_signature(bridge_tools[name].inputSchema)
        assert http_sig == bridge_sig, f"schema drift on tool {name!r}"
        if name in _KNOWN_DESCRIPTION_DIVERGENCE:
            continue
        http_desc = _normalize_description(http_tools[name].description)
        bridge_desc = _normalize_description(bridge_tools[name].description)
        assert http_desc == bridge_desc, f"description drift on tool {name!r}"


async def test_no_tool_description_carries_a_returns_block(state: HubState) -> None:
    """Token diet: the whole tool surface ships to the model on every request.

    The ``Returns:`` blocks restated a result schema the model already sees in
    the tool result itself, at ~1.6k tokens across the two connectors. Only the
    behavioural error codes were worth keeping, on one line.
    """
    server = _build()
    for tools in (await server.list_tools(), await mcp_bridge.mcp.list_tools()):
        for tool in tools:
            assert "Returns:" not in (tool.description or ""), tool.name


async def test_watch_command_answers_without_the_usage_note(state: HubState) -> None:
    """The watcher run/relaunch policy lives in the protocol, not in every result."""
    server = _build()
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")
    res = await _tool(server, "watch_command")(ctx)
    assert set(res) == {"command", "background"}


async def test_session_reaper_sweeps_dead_sessions(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaper sweeps joined sessions whose hub client has died, unlinking token files.

    A *recently active* armed-but-unjoined session (token=None) must NOT be
    swept: it has registered no hub client yet and must stay eligible to join.
    Ageing it out once it goes idle is a separate rule, covered by
    :func:`test_sweep_reaps_idle_unjoined_sessions`.
    """
    import os

    from caucus import mcp_http

    server = _build()
    ctx_joined = _ctx("joined")
    ctx_armed_only = _ctx("armed-only")

    # Arm the second session without joining (a read-only tool arms it).
    await _tool(server, "list_peers")(ctx_armed_only)
    await _tool(server, "join")(ctx_joined, project="alpha")

    # Obtain the token-file path written by watch_command.
    res = await _tool(server, "watch_command")(ctx_joined)
    token_path = str(res["command"]).split("--token-file ", 1)[1]
    assert os.path.exists(token_path)

    # Simulate the joined session's hub client dying.
    monkeypatch.setattr(state, "client_for", lambda _tok: None)

    assert mcp_http._session_reaper_fn is not None
    mcp_http._session_reaper_fn()

    # The joined session's token file must have been unlinked.
    assert not os.path.exists(token_path)

    # The armed-but-unjoined session (token=None) must NOT have been swept.
    who = await _tool(server, "whoami")(ctx_armed_only)
    assert who["joined"] is False


async def test_sweep_reaps_idle_unjoined_sessions(state: HubState) -> None:
    """An armed-but-unjoined record is aged out once it idles past client_ttl.

    Regression: the sweep only ever inspected joined sessions, so a client that
    opened an MCP session, armed with one tool call, then walked away without
    joining leaked its ``_Membership`` for the whole process lifetime.

    The clock is injected (as :meth:`HubState.reap_stale` allows) rather than
    slept through: ``sweep()`` at real-now proves a fresh record survives, and a
    sweep one TTL later proves the idle one does not.
    """
    from caucus import mcp_http

    server = _build()
    idler, joiner = _ctx("idler"), _ctx("joiner")
    await _tool(server, "list_peers")(idler)  # armed, never joined
    await _tool(server, "join")(joiner, project="alpha")

    assert mcp_http._session_reaper_fn is not None
    sweep = mcp_http._session_reaper_fn

    # Both were active a moment ago: a sweep now reaps neither.
    sweep()
    assert (await _tool(server, "whoami")(idler))["armed"] is True
    assert (await _tool(server, "whoami")(joiner))["joined"] is True

    # One TTL past their last tool call, the unjoined record is gone...
    sweep(now=time.time() + state.client_ttl + 1)
    assert (await _tool(server, "whoami")(idler))["armed"] is False
    # ...but the joined one is spared: its liveness is the hub client (kept
    # fresh by the watcher's /receive polls), not last_active. A joined agent
    # that calls no tool for an hour while its watcher listens must survive.
    assert (await _tool(server, "whoami")(joiner))["joined"] is True
