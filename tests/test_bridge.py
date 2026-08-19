"""Integration tests for the MCP bridge tools.

The bridge talks to the hub over real HTTP through a synchronous
``httpx.Client``, so these tests run against the in-thread ``live_hub`` server
rather than an ASGI transport. Each test pins ``PROJECT`` and resets the cached
token via monkeypatch; the ``bridge`` fixture also returns the room to RUNNING
so stop-mode tests don't leak into their neighbours.
"""

from __future__ import annotations

import httpx
import pytest

from caucus import mcp_bridge as bridge_module


@pytest.fixture
def bridge(live_hub: str, monkeypatch: pytest.MonkeyPatch):
    """Point the bridge module at the live hub with a clean, disarmed slate.

    ``_armed`` starts ``False`` so the first tool call arms the session against
    the live hub, exactly as production does — no explicit setup gesture.
    """
    monkeypatch.setattr(bridge_module, "HUB_URL", live_hub)
    monkeypatch.setattr(bridge_module, "_token", None)
    monkeypatch.setattr(bridge_module, "_joined_as", None)
    monkeypatch.setattr(bridge_module, "_armed", False)
    monkeypatch.setattr(bridge_module, "_known_protocol_version", None)
    monkeypatch.setattr(bridge_module, "_protocol_text", None)
    monkeypatch.setattr(bridge_module, "_protocol_delivered", False)
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/control", json={"action": "reset"})
    return bridge_module


def _register_peer(base: str, project: str) -> str:
    """Register a peer straight against the hub and return its token."""
    with httpx.Client(base_url=base, timeout=5.0) as http:
        return str(http.post("/register", json={"project": project}).json()["token"])


# --- lazy arming & gate --------------------------------------------------


def test_first_tool_call_arms_the_session(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Disarmed to start; a read-only tool call arms it against the live hub.
    assert bridge.whoami()["armed"] is False
    result = bridge.list_peers()
    assert "peers" in result
    info = bridge.whoami()
    assert info["armed"] is True
    assert isinstance(info["known_protocol_version"], int)


def test_readonly_tools_work_before_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scout-before-join: read-only tools succeed without joining, just arming.
    assert "peers" in bridge.list_peers()
    assert "channels" in bridge.list_channels()
    assert "forms" in bridge.list_forms()
    assert bridge.ping("nobody")["state"] == "absent"
    assert "floors" in bridge.floor(action="status")


def test_write_tools_require_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    not_joined = {"error": "not_joined", "hint": "call join() first"}
    assert bridge.say("hi") == not_joined
    assert bridge.set_status("busy") == not_joined
    assert bridge.listen(timeout=0) == not_joined
    assert bridge.floor(action="take", reason="x") == not_joined


def test_whoami_is_available_before_arming(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    # whoami never arms or touches the hub, so it stays a pure diagnostic.
    info = bridge.whoami()
    assert info["armed"] is False
    assert info["joined"] is False


# --- HTTP transport ------------------------------------------------------


def test_tool_calls_share_one_keepalive_http_client(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successive tool calls reuse one client instead of reopening a connection.

    The bridge used to build a fresh ``httpx.Client`` per tool call, paying a
    full TCP (and, over a remote hub, TLS) handshake for every say/listen/join.
    """
    monkeypatch.setattr(bridge, "PROJECT", "keepalive")
    bridge.join()
    with bridge._client() as first:
        pass
    bridge.list_peers()
    bridge.list_channels()
    with bridge._client() as second:
        pass

    assert second is first
    assert not first.is_closed  # the borrow must not close the shared client


def test_repointing_the_hub_url_rebuilds_the_client(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached client is never reused against a different hub address."""
    with bridge._client() as original:
        pass
    monkeypatch.setattr(bridge, "HUB_URL", "http://127.0.0.1:1")
    with bridge._client() as rebuilt:
        pass

    assert rebuilt is not original
    assert original.is_closed


def test_join_flags_stale_protocol_when_behind(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "stale-joiner")
    # Arm, then pretend the session learned an ancient revision so join is stale.
    bridge.list_peers()
    monkeypatch.setattr(bridge, "_known_protocol_version", 0)
    result = bridge.join()
    assert result["joined"] is True
    assert result["protocol_stale"] is True
    assert "Caucus operating protocol" in result["protocol"]


def test_join_is_current_and_delivers_protocol(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "fresh-joiner")
    result = bridge.join()
    assert result["joined"] is True
    assert result["protocol_stale"] is False
    # join always hands back the protocol now that there is no setup step.
    assert "Caucus operating protocol" in result["protocol"]


def test_rejoin_after_protocol_upgrade_keeps_serving_the_new_text(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the stale branch must refresh the cached protocol text.

    Arming caches the hub's text and revision. When the hub then ships a new
    revision, the first join is flagged stale and hands back the fresh text —
    but if it advances ``_known_protocol_version`` without refreshing
    ``_protocol_text``, the *second* join computes ``stale=False`` and serves the
    superseded text under the new version label. The second join below is the one
    that catches it — it asks for the text explicitly, since an unforced re-join
    no longer re-sends what the session has already read.
    """
    from caucus import hub as hub_module

    monkeypatch.setattr(bridge, "PROJECT", "upgrade-joiner")
    # Arm against the hub's current revision, caching that text.
    bridge.list_peers()
    armed_at = bridge._known_protocol_version
    assert isinstance(armed_at, int)

    # The hub ships a new protocol revision (as a restart carrying a bump would).
    new_version = armed_at + 1
    new_text = "Caucus operating protocol (upgraded revision)"
    monkeypatch.setattr(hub_module, "PROTOCOL_VERSION", new_version)
    monkeypatch.setattr(hub_module, "PROTOCOL_TEXT", new_text)

    first = bridge.join()
    assert first["protocol_stale"] is True
    assert first["protocol_version"] == new_version
    assert first["protocol"] == new_text

    # Second join: no longer stale, so the bridge answers from its own cache —
    # which must hold the NEW text, not the one cached at arming time.
    second = bridge.join(force_protocol=True)
    assert second["protocol_stale"] is False
    assert second["protocol_version"] == new_version
    assert second["protocol"] == new_text


def test_second_join_omits_the_protocol_text(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ~4.4k-token manual is delivered once per session, not per join."""
    monkeypatch.setattr(bridge, "PROJECT", "thrifty-joiner")
    first = bridge.join()
    assert "Caucus operating protocol" in first["protocol"]

    second = bridge.join()
    assert "protocol" not in second
    assert second["protocol_stale"] is False
    assert second["protocol_version"] == first["protocol_version"]
    assert "already delivered this session" in second["note"]
    # The note must name the way out, or it is a dead end for an agent whose
    # context was compacted and no longer holds the manual.
    assert "force_protocol=true" in second["note"]


def test_join_without_a_cached_protocol_does_not_claim_delivery(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undelivered protocol must never be reported as already delivered.

    The note is gated on the delivery flag rather than on "we sent no text this
    call": an empty cache used to fall through to the reassuring branch, leaving
    the agent with no manual and no hint that one exists.
    """
    monkeypatch.setattr(bridge, "PROJECT", "textless-joiner")
    # Arm normally, then simulate an empty cache with nothing ever delivered.
    bridge.list_peers()
    monkeypatch.setattr(bridge, "_protocol_text", None)
    monkeypatch.setattr(bridge, "_protocol_delivered", False)

    result = bridge.join()
    assert result["joined"] is True
    assert "protocol" not in result
    assert "already delivered" not in result["note"]
    assert "unavailable" in result["note"]
    assert "force_protocol=true" in result["note"]


def test_stale_join_keeps_the_cached_text_when_the_hub_sends_none(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale join with no ``protocol_text`` must not evict the good cached copy."""
    monkeypatch.setattr(bridge, "PROJECT", "textless-refresh")
    first = bridge.join()
    cached = first["protocol"]
    assert "Caucus operating protocol" in cached

    # The hub bumps the revision but serves an empty body for it, so the join is
    # flagged stale and carries no usable text.
    from caucus import hub as hub_module

    monkeypatch.setattr(
        hub_module, "PROTOCOL_VERSION", hub_module.PROTOCOL_VERSION + 1
    )
    monkeypatch.setattr(hub_module, "PROTOCOL_TEXT", "")

    second = bridge.join()
    assert second["protocol_stale"] is True
    # The cache survived, so the agent still gets a manual rather than null.
    assert second["protocol"] == cached


def test_force_protocol_redelivers_the_text(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """force_protocol is the post-compaction escape hatch: it re-sends the text."""
    monkeypatch.setattr(bridge, "PROJECT", "forceful-joiner")
    bridge.join()
    forced = bridge.join(force_protocol=True)
    assert "Caucus operating protocol" in forced["protocol"]
    assert "already delivered" not in str(forced.get("note", ""))


def test_stale_protocol_redelivers_without_force(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revision bump still pushes the new text on the next join, unasked."""
    from caucus import hub as hub_module

    monkeypatch.setattr(bridge, "PROJECT", "bumped-joiner")
    first = bridge.join()
    assert "protocol" in first

    new_text = "Caucus operating protocol (bumped revision)"
    monkeypatch.setattr(
        hub_module, "PROTOCOL_VERSION", hub_module.PROTOCOL_VERSION + 1
    )
    monkeypatch.setattr(hub_module, "PROTOCOL_TEXT", new_text)

    second = bridge.join()
    assert second["protocol_stale"] is True
    assert second["protocol"] == new_text
    assert "re-read the protocol" in second["note"]


def test_tool_reports_hub_unreachable_when_arming_fails(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arming gate fails closed: a dead hub yields a structured error.

    Distinct from the ``_resilient_hub_call`` decorator (tests/test_security_low.py):
    here the very first tool call cannot fetch /protocol, so ``_ensure_armed``
    itself returns the error and the tool body never runs.
    """
    dead_hub = "http://127.0.0.1:1"
    monkeypatch.setattr(bridge, "HUB_URL", dead_hub)
    result = bridge.list_peers()
    assert result["error"] == "hub_unreachable"
    assert result["hub"] == dead_hub
    assert "detail" in result
    # The failed arming left the session disarmed, so a retry can still arm.
    assert bridge.whoami()["armed"] is False


def test_join_surfaces_automode_under_claude_code(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude-Code auto-mode detection moved from the old setup() gesture into
    join() (see mcp_bridge.py's automode.is_claude_code() gate); under a
    Claude Code host, join's result must still carry the ``automode`` block.
    """
    monkeypatch.setattr(bridge, "PROJECT", "automode-joiner")
    monkeypatch.setattr(bridge.automode, "is_claude_code", lambda: True)
    monkeypatch.setattr(
        bridge.automode, "detect", lambda: {"operator_rule": "missing"}
    )
    result = bridge.join()
    assert result["joined"] is True
    assert result["automode"] == {"operator_rule": "missing"}


def test_join_omits_automode_outside_claude_code(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-Claude-Code hosts (Codex, Gemini, ...) get no automode noise on join."""
    monkeypatch.setattr(bridge, "PROJECT", "non-automode-joiner")
    monkeypatch.setattr(bridge.automode, "is_claude_code", lambda: False)
    result = bridge.join()
    assert result["joined"] is True
    assert "automode" not in result


def test_join_survives_automode_detect_failure(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken auto-mode probe must never fail the join itself (non-fatal)."""
    monkeypatch.setattr(bridge, "PROJECT", "automode-boom-joiner")
    monkeypatch.setattr(bridge.automode, "is_claude_code", lambda: True)

    def _boom() -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(bridge.automode, "detect", _boom)
    result = bridge.join()
    assert result["joined"] is True
    assert result["automode"] == {"operator_rule": "unknown"}


# --- identity ------------------------------------------------------------


def test_whoami_before_join(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "solo")
    info = bridge.whoami()
    assert info["default_project"] == "solo"
    assert info["joined_as"] is None
    assert info["hub"] == bridge.HUB_URL
    assert info["joined"] is False


def test_join_then_whoami_is_joined(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "reg-test")
    result = bridge.join()
    assert result["joined"] is True
    assert result["project"] == "reg-test"
    info = bridge.whoami()
    assert info["joined"] is True
    assert info["joined_as"] == "reg-test"


def test_join_with_explicit_name_overrides_default(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "from-env")
    bridge.join(project="explicit-name")
    assert bridge.whoami()["joined_as"] == "explicit-name"
    assert "explicit-name" in bridge.list_peers()["peers"]


def test_leave_clears_membership(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "leaver")
    bridge.join()
    assert bridge.whoami()["joined"] is True
    result = bridge.leave()
    assert result["left"] is True
    assert bridge.whoami()["joined"] is False
    assert bridge.say("nope") == {"error": "not_joined", "hint": "call join() first"}


def test_leave_deregisters_from_hub_roster(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "hub-leaver")
    bridge.join()
    assert "hub-leaver" in bridge.list_peers()["peers"]
    bridge.leave()
    # The hub dropped the peer at once, not just the local token cache.
    assert "hub-leaver" not in bridge.list_peers()["peers"]


def test_list_peers_includes_self(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "peers-test")
    bridge.join()
    assert "peers-test" in bridge.list_peers()["peers"]


# --- ping & status -------------------------------------------------------


def test_ping_absent_peer(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "pinger")
    # ping needs no join — scout before entering.
    result = bridge.ping("nobody-here")
    assert result == {"peer": "nobody-here", "state": "absent", "present": False}


def test_ping_reports_live_peer(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_peer(live_hub, "live-peer")
    monkeypatch.setattr(bridge, "PROJECT", "pinger")
    result = bridge.ping("live-peer")
    assert result["state"] == "live"
    assert result["present"] is True


def test_set_status_without_join_errors(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_token", None)
    assert bridge.set_status("busy") == {
        "error": "not_joined",
        "hint": "call join() first",
    }


def test_set_status_then_ping_surfaces_it(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "statuser")
    bridge.join()
    assert bridge.set_status("implementing the parser") == {
        "status": "implementing the parser"
    }
    assert bridge.ping("statuser")["status"] == "implementing the parser"


def test_set_status_blank_clears(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "clearer")
    bridge.join()
    bridge.set_status("busy")
    assert bridge.set_status("") == {"status": None}
    assert bridge.ping("clearer")["status"] is None


# --- say -----------------------------------------------------------------


def test_say_without_join_errors(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "noauth")
    assert bridge.say("hi") == {"error": "not_joined", "hint": "call join() first"}


def test_say_direct_is_delivered(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher = _register_peer(live_hub, "watcher-1")
    monkeypatch.setattr(bridge, "PROJECT", "sayer-1")
    bridge.join()

    result = bridge.say("hello watcher", to="watcher-1")
    assert "message_id" in result
    assert result["delivered_to"] == ["watcher-1"]

    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        got = http.get(
            "/receive", params={"token": watcher, "timeout": 3}
        ).json()
    assert any("hello watcher" in m["content"] for m in got["messages"])


def test_say_is_rate_limited_under_flood(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "flooder")
    bridge.join()
    results = [bridge.say(f"spam {i}") for i in range(20)]
    assert any(r.get("error") == "rate_limited" for r in results)
    rate_limited = next(r for r in results if r.get("error") == "rate_limited")
    assert "retry_after" in rate_limited


def test_say_when_stopped_reports_stopped(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "stopper")
    bridge.join()
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/control", json={"action": "stop"})
    result = bridge.say("should not pass")
    assert result.get("stopped") is True


# --- listen --------------------------------------------------------------


def test_listen_returns_chatter(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "listener-1")
    bridge.join()

    peer = _register_peer(live_hub, "peer-x")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post(
            "/send",
            json={"token": peer, "to": "listener-1", "content": "ping for you"},
        )

    result = bridge.listen(timeout=3)
    assert result["stop"] is False
    assert any("ping for you" in m["content"] for m in result["messages"])


def test_listen_returns_a_lean_message_shape(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary chatter comes back as sender/recipient/content and nothing else.

    The ``/receive`` envelope (``id``, ``ts``, ``seq``, plus a ``kind`` and
    ``origin`` that only restate the default) is bookkeeping the bridge uses to
    ACK. Passing it to the agent charged the model for context it never reads,
    once per message.
    """
    monkeypatch.setattr(bridge, "PROJECT", "lean-listener")
    bridge.join()

    peer = _register_peer(live_hub, "lean-peer")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post(
            "/send",
            json={"token": peer, "to": "lean-listener", "content": "trimmed"},
        )

    messages = bridge.listen(timeout=3)["messages"]
    assert len(messages) == 1
    assert messages[0] == {
        "sender": "lean-peer",
        "recipient": "lean-listener",
        "content": "trimmed",
    }


def test_listen_keeps_kind_and_meta_on_an_answer(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-form answer is not ordinary chatter, so it keeps its markers.

    Answering a form is an operator-console gesture, so the answer is submitted
    straight against the live hub's state rather than over the ``/ui`` socket.
    """
    from caucus import hub as hub_module

    monkeypatch.setattr(bridge, "PROJECT", "form-asker")
    bridge.join()

    form_id = bridge.ask_operator(
        title="Ship it?",
        fields=[{"key": "go", "label": "Go?", "type": "radio", "options": ["y", "n"]}],
    )["form_id"]
    hub_module.state.answer_form(form_id, {"go": "y"})

    answers = [m for m in bridge.listen(timeout=3)["messages"] if m.get("meta")]
    assert answers, "the form answer never came back"
    assert answers[0]["kind"] == "answer"
    # Operator-issued, so the trust flag must survive the trim.
    assert answers[0]["origin"] == "operator"
    assert answers[0]["meta"]["form_id"] == form_id


def test_listen_keeps_origin_on_an_operator_message(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human-origin message keeps ``origin``: it is the anti-spoofing signal."""
    from caucus import hub as hub_module
    from caucus.models import Message

    monkeypatch.setattr(bridge, "PROJECT", "human-listener")
    bridge.join()

    hub_module.state.route(
        Message(
            sender="human",
            recipient="human-listener",
            content="operator here",
            origin="operator",
        )
    )

    messages = bridge.listen(timeout=3)["messages"]
    spoken = [m for m in messages if m["content"] == "operator here"]
    assert spoken, "the operator message never arrived"
    assert spoken[0]["origin"] == "operator"


# --- talking stick -------------------------------------------------------


def test_take_floor_then_status_reports_it(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "stick-holder")
    bridge.join()
    taken = bridge.floor(action="take", reason="prod is down")
    assert taken["ok"] is True
    assert taken["holder"] == "stick-holder"
    status = bridge.floor(action="status")
    assert status["floors"]["all"]["holder"] == "stick-holder"
    # Release so the module-scoped hub does not carry the stick into the next test.
    assert bridge.floor(action="drop")["released"] is True


def test_say_is_blocked_while_another_holds_the_floor(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _register_peer(live_hub, "floor-holder")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post(
            "/floor",
            json={"token": holder, "action": "take", "scope": "all", "reason": "fire"},
        )
    monkeypatch.setattr(bridge, "PROJECT", "barred")
    bridge.join()
    try:
        result = bridge.say("let me in")
        assert result["error"] == "floor_held"
        assert result["held_by"] == "floor-holder"
        # The barred peer can still queue for the next turn.
        assert bridge.floor(action="raise")["ok"] is True
    finally:
        with httpx.Client(base_url=live_hub, timeout=5.0) as http:
            http.post("/floor", json={"token": holder, "action": "drop", "scope": "all"})


def test_take_floor_without_join_errors(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert bridge.floor(action="take", reason="x") == {
        "error": "not_joined",
        "hint": "call join() first",
    }


def test_floor_action_raise_queues_behind_the_holder(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _register_peer(live_hub, "raise-holder")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post(
            "/floor",
            json={"token": holder, "action": "take", "scope": "all", "reason": "x"},
        )
    monkeypatch.setattr(bridge, "PROJECT", "raise-waiter")
    bridge.join()
    try:
        assert bridge.floor(action="raise") == {"ok": True, "scope": "all", "position": 1}
    finally:
        with httpx.Client(base_url=live_hub, timeout=5.0) as http:
            http.post("/floor", json={"token": holder, "action": "drop", "scope": "all"})


def test_floor_action_pass_hands_the_stick_to_the_next_hand(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiter = _register_peer(live_hub, "pass-waiter")
    monkeypatch.setattr(bridge, "PROJECT", "pass-holder")
    bridge.join()
    bridge.floor(action="take", reason="mine first")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/floor", json={"token": waiter, "action": "raise", "scope": "all"})
    result = bridge.floor(action="pass")
    # The stick genuinely moved rather than merely being released.
    assert result["passed_to"] == "pass-waiter"
    assert bridge.floor(action="status")["floors"]["all"]["holder"] == "pass-waiter"
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/floor", json={"token": waiter, "action": "drop", "scope": "all"})


def test_floor_action_drop_reopens_the_lane(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "dropper")
    bridge.join()
    bridge.floor(action="take", reason="brief crisis")
    assert bridge.floor(action="drop")["released"] is True
    # Dropped outright, not passed on: the lane is fully open again.
    assert bridge.floor(action="status")["floors"] == {}


def test_floor_rejects_unknown_action(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "floor-bad-action")
    bridge.join()
    result = bridge.floor(action="wiggle")
    assert result["error"] == "invalid_action"


def test_listen_quiet_poll_is_empty(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "quiet-listener")
    bridge.join()
    result = bridge.listen(timeout=0)
    assert result["messages"] == []
    assert result["stop"] is False


def test_listen_surfaces_stop(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "stop-listener")
    bridge.join()
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/control", json={"action": "stop"})

    result = bridge.listen(timeout=3)
    assert result["stop"] is True
    # The control signal is folded into the stop flag, not the chatter list.
    assert all(m.get("kind") != "control" for m in result["messages"])


# --- channels ------------------------------------------------------------


def test_join_channel_subscribes(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "ch-joiner")
    bridge.join()
    result = bridge.join_channel("#br-room")
    assert result == {"joined": True, "channel": "#br-room"}
    assert "#br-room" in bridge.list_channels()["channels"]


def test_join_channel_rejects_bad_name(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "ch-bad")
    bridge.join()
    assert bridge.join_channel("noprefix") == {
        "error": "invalid_channel",
        "hint": "channel must start with '#'",
    }


def test_say_to_channel_reaches_member(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _register_peer(live_hub, "br-ch-rx")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/channels/join", json={"token": peer, "channel": "#br-deliver"})

    monkeypatch.setattr(bridge, "PROJECT", "br-ch-tx")
    bridge.join()
    result = bridge.say("hi channel", to="#br-deliver")
    assert "br-ch-rx" in result["delivered_to"]

    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        got = http.get("/receive", params={"token": peer, "timeout": 3}).json()
    assert any("hi channel" in m["content"] for m in got["messages"])


def test_leave_channel_unsubscribes(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "ch-leaver")
    bridge.join()
    bridge.join_channel("#br-leave")
    result = bridge.leave_channel("#br-leave")
    assert result == {"left": True, "channel": "#br-leave"}


def test_list_channels_works_before_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read-only scout: list_channels arms and answers without a join.
    assert "channels" in bridge.list_channels()


def test_channel_membership_tools_require_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "unjoined-ch")
    not_joined = {"error": "not_joined", "hint": "call join() first"}
    assert bridge.join_channel("#x") == not_joined
    assert bridge.leave_channel("#x") == not_joined


def test_join_channel_is_rate_limited_under_flood(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "ch-flooder")
    bridge.join()
    results = [bridge.join_channel(f"#c{i}") for i in range(20)]
    assert any(r.get("error") == "rate_limited" for r in results)
    rate_limited = next(r for r in results if r.get("error") == "rate_limited")
    assert "retry_after" in rate_limited


def test_set_channel_topic_as_member(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "topic-setter")
    bridge.join()
    bridge.join_channel("#br-topic")
    result = bridge.set_channel_topic("#br-topic", "the topic")
    assert result == {"channel": "#br-topic", "topic": "the topic"}


def test_set_channel_topic_non_member_errors(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "topic-outsider")
    bridge.join()
    result = bridge.set_channel_topic("#br-foreign", "nope")
    assert result == {"error": "not_a_member", "hint": "join the channel first"}


def test_join_surfaces_channel_directory(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _register_peer(live_hub, "br-dir-peer")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/channels/join", json={"token": peer, "channel": "#br-dir"})
        http.post(
            "/channels/topic",
            json={"token": peer, "channel": "#br-dir", "topic": "dir topic"},
        )
    monkeypatch.setattr(bridge, "PROJECT", "br-dir-joiner")
    result = bridge.join()
    channels = result["channels"]
    assert channels["#br-dir"]["topic"] == "dir topic"


# --- operator forms ------------------------------------------------------


def _radio_field() -> dict[str, object]:
    return {"key": "ok", "label": "Proceed?", "type": "radio", "options": ["yes", "no"]}


def test_ask_operator_opens_form(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "form-asker")
    bridge.join()
    result = bridge.ask_operator("Deploy?", [_radio_field()])
    assert result["form_id"]
    assert result["to"] == "all"
    assert any(f["title"] == "Deploy?" for f in bridge.list_forms()["forms"])


def test_ask_operator_requires_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "form-unjoined")
    assert bridge.ask_operator("t", [_radio_field()]) == {
        "error": "not_joined",
        "hint": "call join() first",
    }


def test_ask_operator_rejects_bad_target(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "form-bad-target")
    bridge.join()
    result = bridge.ask_operator("t", [_radio_field()], to="some-peer")
    assert result["error"] == "invalid_form"


def test_list_forms_reflects_pending(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "form-lister")
    bridge.join()
    bridge.ask_operator("Pending one", [_radio_field()])
    titles = [f["title"] for f in bridge.list_forms()["forms"]]
    assert "Pending one" in titles


def test_list_forms_works_before_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read-only scout: list_forms arms and answers without a join; ask_operator
    # still needs one.
    assert "forms" in bridge.list_forms()
    assert bridge.ask_operator("t", [_radio_field()]) == {
        "error": "not_joined",
        "hint": "call join() first",
    }


# --- watch_command -------------------------------------------------------


def test_watch_command_requires_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "unjoined")
    assert bridge.watch_command() == {
        "error": "not_joined",
        "hint": "call join() first",
    }


def test_watch_command_returns_runnable_command(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setattr(bridge, "PROJECT", "watcher-host")
    bridge.join()
    result = bridge.watch_command()
    assert result["background"] is True
    command = result["command"]
    assert isinstance(command, str)
    assert command.startswith("caucus-watch ")
    assert f"--hub {bridge.HUB_URL}" in command
    # The run/relaunch/stop policy lives in the protocol, not in every result.
    assert set(result) == {"command", "background"}
    # The token travels by file path, never inline in the command/transcript.
    assert "--token " not in command
    assert "--token-file " in command
    token_path = bridge._token_file
    assert token_path is not None and os.path.exists(token_path)
    with open(token_path, encoding="utf-8") as fh:
        assert fh.read().strip() == bridge._token
    # And the file is owner-only (0600).
    assert (os.stat(token_path).st_mode & 0o777) == 0o600


def test_join_hands_back_the_watch_command(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """join() folds in what watch_command() would return, sparing a round-trip.

    Starting the watcher is the documented next step after joining, so making
    the agent spend a second tool call on watch_command() was a turn for nothing.
    """
    import os

    monkeypatch.setattr(bridge, "PROJECT", "watch-in-join")
    joined = bridge.join()
    folded = joined["watch"]
    assert isinstance(folded, str)
    assert folded.startswith(f"caucus-watch --hub {bridge.HUB_URL} --token-file ")

    # Same shape watch_command() mints, token file and all — one code path.
    minted = bridge.watch_command()["command"]
    assert minted.rsplit("--token-file ", 1)[0] == folded.rsplit("--token-file ", 1)[0]
    path = bridge._token_file
    assert path is not None and os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        assert fh.read().strip() == bridge._token
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_watch_command_does_not_invalidate_the_command_join_handed_back(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: calling watch_command() must not kill join()'s watch command.

    ``caucus-watch`` reads ``--token-file`` once at startup and exits on a read
    failure, so rotating the file on every mint meant the command from join's
    ``watch`` field pointed at a deleted path the moment watch_command() ran —
    and running both is precisely the sequence the protocol describes. The live
    file is reused while the token is unchanged, so both commands stay runnable.
    """
    import os

    monkeypatch.setattr(bridge, "PROJECT", "watch-file-keeper")
    from_join = str(bridge.join()["watch"])
    from_tool = str(bridge.watch_command()["command"])

    assert from_join == from_tool  # same token, same file, one command
    path = from_join.split("--token-file ", 1)[1]
    assert os.path.exists(path), "join's watch command lost its token file"
    with open(path, encoding="utf-8") as fh:
        assert fh.read().strip() == bridge._token


def test_watch_command_mints_a_fresh_file_when_the_token_changes(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse is keyed on the token: a new identity must not poll with the old one."""
    import os

    monkeypatch.setattr(bridge, "PROJECT", "watch-rotator")
    first = str(bridge.join()["watch"])
    first_path = first.split("--token-file ", 1)[1]

    # Leave and re-join under a different name: a different token, so the file
    # must be replaced rather than reused.
    bridge.leave()
    assert not os.path.exists(first_path)  # leave() cleans up behind itself
    second = str(bridge.join(project="watch-rotator-2")["watch"])
    second_path = second.split("--token-file ", 1)[1]

    assert second_path != first_path
    with open(second_path, encoding="utf-8") as fh:
        assert fh.read().strip() == bridge._token


def test_watch_command_replaces_a_token_file_deleted_underneath_it(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished file is re-minted, so the command handed back is always runnable."""
    import os

    monkeypatch.setattr(bridge, "PROJECT", "watch-file-loser")
    bridge.join()
    os.unlink(bridge._token_file)  # something outside the bridge removed it

    path = str(bridge.watch_command()["command"]).split("--token-file ", 1)[1]
    assert os.path.exists(path)


def test_leave_deletes_watcher_token_file(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setattr(bridge, "PROJECT", "watcher-leaver")
    bridge.join()
    bridge.watch_command()
    path = bridge._token_file
    assert path is not None and os.path.exists(path)
    bridge.leave()
    assert bridge._token_file is None
    assert not os.path.exists(path)


# --- duplicate-join protection -------------------------------------------


def test_rejoin_same_bridge_sends_token_and_is_reaffirmed(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second join() from the same bridge re-sends the cached token.

    The hub sees the matching token and returns REAFFIRMED (200), so the
    result carries ``joined: True`` and the bridge still holds the same
    project identity. This proves the token-reuse path prevents the
    bridge from being refused as a duplicate of itself.
    """
    monkeypatch.setattr(bridge, "PROJECT", "reaffirm-me")
    first = bridge.join()
    assert first["joined"] is True
    token_after_first = bridge._token

    # Second join — the cached token is threaded through the POST body.
    second = bridge.join()
    assert second["joined"] is True
    assert second["project"] == "reaffirm-me"
    # The hub reaffirms: the token must stay the same (no new one issued).
    assert bridge._token == token_after_first


def test_join_returns_name_in_use_when_live_listener_holds_name(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh join() without a matching token is refused with name_in_use.

    Simulate a live listener: register a peer directly, then spin a
    background thread that holds a ``/receive`` long-poll so
    ``active_polls > 0`` on the hub side.  A second bridge join (with no
    cached token) must get back ``{"error": "name_in_use", ...}``.
    """
    import threading

    name = "contested-peer"

    # Register the peer and grab its token.
    token = _register_peer(live_hub, name)

    # Hold a long-poll in the background so active_polls becomes 1.
    stop_event = threading.Event()

    def _hold_poll() -> None:
        with httpx.Client(base_url=live_hub, timeout=10.0) as http:
            try:
                http.get("/receive", params={"token": token, "timeout": 5})
            except Exception:
                pass
        stop_event.set()

    poller = threading.Thread(target=_hold_poll, daemon=True)
    poller.start()

    # Give the poll a moment to arrive at the hub so active_polls is set.
    import time

    time.sleep(0.15)

    # Clear any cached token so this bridge looks like a fresh/different process.
    monkeypatch.setattr(bridge, "_token", None)
    monkeypatch.setattr(bridge, "PROJECT", name)

    result = bridge.join()

    # Clean up the poll thread.
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/leave", json={"token": token})
    poller.join(timeout=3.0)

    assert result.get("error") == "name_in_use"
    assert result.get("project") == name
    assert "note" in result
    assert result.get("hub") == live_hub
    # Bridge must not have updated its own membership on a refused join.
    assert bridge._token is None
    assert bridge._joined_as is None
