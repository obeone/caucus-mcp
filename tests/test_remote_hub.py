"""Tests for making a hub reachable from other machines.

Four surfaces, all introduced together because none of them is usable alone:

* ``--allowed-host`` / ``CAUCUS_ALLOWED_HOSTS`` feeding the ``/mcp``
  DNS-rebinding guard (:func:`caucus.hub._collect_allowed_hosts`),
* ``--public-url`` / ``CAUCUS_PUBLIC_URL`` and its validation
  (:func:`caucus.urlguard.validate_public_url`),
* the ``watch_command`` tool handing a remote agent a runnable command instead
  of a path on the hub's own filesystem, and the single-use watch ticket that
  keeps the peer token out of that command,
* the startup refusal on a non-loopback bind without both credentials.

The refusal tests drive :func:`caucus.hub.main` itself with ``uvicorn.run``
stubbed out, because the gate is part of the CLI contract and a pure-function
test would not prove the flag is wired to it.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from caucus import hub as hub_module
from caucus.hub import AuthConfig, ServerConfig, _collect_allowed_hosts
from caucus.mcp_http import build_mcp_server
from caucus.state import WATCH_TICKET_TTL, HubState
from caucus.urlguard import ALLOW_REMOTE_ENV, validate_hub_url, validate_public_url

# ---------------------------------------------------------------------------
# --allowed-host / CAUCUS_ALLOWED_HOSTS
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_allowed_hosts_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an operator's real environment out of every case in this module."""
    monkeypatch.delenv("CAUCUS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("CAUCUS_PUBLIC_URL", raising=False)


def test_bare_host_is_allowed_on_the_hubs_own_port() -> None:
    """A hostname with no port means "this hub, under that name"."""
    assert _collect_allowed_hosts(["hub.lan"], 8765) == ["hub.lan:8765"]


def test_explicit_host_port_is_kept_verbatim() -> None:
    """An entry that already names a port is a deliberate, different address."""
    assert _collect_allowed_hosts(["hub.lan:443"], 8765) == ["hub.lan:443"]


def test_bracketed_ipv6_without_a_port_gets_one() -> None:
    """``[2001:db8::1]`` is a bare host, not a host that carries a port."""
    assert _collect_allowed_hosts(["[2001:db8::1]"], 8765) == ["[2001:db8::1]:8765"]


def test_bracketed_ipv6_with_a_port_is_kept_verbatim() -> None:
    """The closing bracket is not last, so the trailing colon is the port."""
    assert _collect_allowed_hosts(["[2001:db8::1]:9000"], 8765) == [
        "[2001:db8::1]:9000"
    ]


def test_repeated_flags_accumulate_in_order() -> None:
    """``--allowed-host`` is repeatable, like ``--allowed-origin``."""
    assert _collect_allowed_hosts(["a.lan", "b.lan:80"], 8765) == [
        "a.lan:8765",
        "b.lan:80",
    ]


def test_env_var_is_comma_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env form mirrors ``CAUCUS_ALLOWED_ORIGINS``: comma-separated."""
    monkeypatch.setenv("CAUCUS_ALLOWED_HOSTS", "a.lan, b.lan:80 ,")
    assert _collect_allowed_hosts(None, 8765) == ["a.lan:8765", "b.lan:80"]


def test_flags_and_env_merge_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming the same host twice, by either route, yields one entry."""
    monkeypatch.setenv("CAUCUS_ALLOWED_HOSTS", "hub.lan,other.lan")
    assert _collect_allowed_hosts(["hub.lan:8765"], 8765) == [
        "hub.lan:8765",
        "other.lan:8765",
    ]


def test_no_flags_and_no_env_is_empty() -> None:
    """The default stays the loopback-only posture the guard already has."""
    assert _collect_allowed_hosts(None, 8765) == []


@pytest.mark.parametrize(
    ("entry", "expected"),
    [("::1", "[::1]:8765"), ("2001:db8::1", "[2001:db8::1]:8765")],
)
def test_bare_ipv6_is_bracketed_and_given_the_hubs_port(
    entry: str, expected: str
) -> None:
    """A Host header always brackets an IPv6 literal, so the entry must too.

    Passed through verbatim, ``--allowed-host ::1`` matches no Host header the
    guard will ever see, and the operator believes they allowed an address they
    did not.
    """
    assert _collect_allowed_hosts([entry], 8765) == [expected]


# ---------------------------------------------------------------------------
# --public-url validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://hub.example.net", "https://hub.example.net"),
        ("http://hub.lan:8765", "http://hub.lan:8765"),
        # A trailing slash is the empty path spelled out; strip it so the hub
        # never builds "https://host//receive".
        ("https://hub.example.net/", "https://hub.example.net"),
        ("HTTPS://hub.example.net", "HTTPS://hub.example.net"),
        ("http://[2001:db8::1]:8765", "http://[2001:db8::1]:8765"),
    ],
)
def test_public_url_accepts_a_bare_origin(url: str, expected: str) -> None:
    """Scheme plus host (plus optional port) is the whole accepted shape."""
    assert validate_public_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://hub.example.net",
        "ws://hub.example.net",
        "hub.example.net:8765",  # no scheme: urlparse reads "hub.example.net"
        "https://",
        "https://hub.example.net/caucus",
        "https://hub.example.net/?x=1",
        "https://hub.example.net#frag",
    ],
)
def test_public_url_rejects_anything_else(url: str) -> None:
    """A wrong scheme, a missing host, or anything past the origin is refused."""
    with pytest.raises(ValueError):
        validate_public_url(url)


# ---------------------------------------------------------------------------
# watch_command: loopback keeps the token file, remote gets the env form
# ---------------------------------------------------------------------------


def _ctx(session_id: str) -> Any:
    """Minimal ``Context`` stand-in carrying an ``Mcp-Session-Id`` header."""
    request = type("_Req", (), {"headers": {"mcp-session-id": session_id}})()
    request_context = type("_RC", (), {"request": request})()
    return type("_Ctx", (), {"request_context": request_context})()


def _tool(server: Any, name: str) -> Any:
    """Return a registered tool's underlying callable for a direct call."""
    return server._tool_manager.get_tool(name).fn


async def test_watch_command_loopback_keeps_the_token_file(state: HubState) -> None:
    """The default deployment is unchanged: a 0600 token file on this machine."""
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

    await _tool(server, "leave")(ctx)
    assert not os.path.exists(path)


async def test_watch_command_remote_hands_out_a_ticket_not_the_token(
    state: HubState,
) -> None:
    """A remote agent gets a claim check, never the room bearer itself.

    The peer token opens ``/receive``, ``/send``, ``/ack``, ``/channels/*``,
    ``/ask`` and ``/floor``, and the agent key gates none of them. Printing it
    in a shell command would put it in the agent's transcript, its shell
    history and the watcher's environ, which is exactly what the loopback
    token file exists to prevent.
    """
    server = build_mcp_server(
        hub_module.app, self_url="https://hub.example.net", remote=True
    )
    ctx = _ctx("s1")
    joined = await _tool(server, "join")(ctx, project="alpha")

    res = await _tool(server, "watch_command")(ctx)
    command = str(res["command"])
    prefix, _, ticket = command.partition(" --ticket ")
    assert prefix == "caucus-watch --hub https://hub.example.net"
    assert ticket
    # The ticket is real: it redeems to this peer's live token, once.
    token = state.redeem_watch_ticket(ticket)
    assert token is not None
    client = state.client_for(token)
    assert client is not None and client.project == "alpha"
    # And the token itself appears nowhere in the whole result payload, not
    # merely outside the command string.
    assert token not in repr(res)
    assert "CAUCUS_TOKEN" not in command
    # Nothing naming a path on the hub's filesystem either.
    assert "--token-file" not in command
    # join()'s own payload must not leak it as a consolation prize.
    assert token not in repr(joined)


async def test_watch_command_refresh_revokes_the_previous_ticket(
    state: HubState,
) -> None:
    """Refreshing must retire the old ticket, not just mint a new one beside it.

    Without this, every ``watch_command()`` call left the prior ticket
    outstanding for its full TTL: N calls meant N live bearer credentials for
    the same peer token, each one already sitting in the agent's transcript.
    """
    server = build_mcp_server(
        hub_module.app, self_url="https://hub.example.net", remote=True
    )
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")

    first = str((await _tool(server, "watch_command")(ctx))["command"])
    _, _, first_ticket = first.partition(" --ticket ")
    second = str((await _tool(server, "watch_command")(ctx))["command"])
    _, _, second_ticket = second.partition(" --ticket ")

    assert first_ticket and second_ticket and first_ticket != second_ticket
    # The retired ticket is dead...
    assert state.redeem_watch_ticket(first_ticket) is None
    # ...while the fresh one still works.
    assert state.redeem_watch_ticket(second_ticket) is not None


async def test_leave_revokes_the_outstanding_watch_ticket(
    state: HubState,
) -> None:
    """A departing agent must not leave a live ticket for the token it dropped."""
    server = build_mcp_server(
        hub_module.app, self_url="https://hub.example.net", remote=True
    )
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")
    command = str((await _tool(server, "watch_command")(ctx))["command"])
    _, _, ticket = command.partition(" --ticket ")
    assert ticket

    await _tool(server, "leave")(ctx)

    assert state.redeem_watch_ticket(ticket) is None


async def test_dead_session_sweep_revokes_its_ticket(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session reaped as dead must not leave its ticket redeemable behind it."""
    from caucus import mcp_http

    server = build_mcp_server(
        hub_module.app, self_url="https://hub.example.net", remote=True
    )
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")
    command = str((await _tool(server, "watch_command")(ctx))["command"])
    _, _, ticket = command.partition(" --ticket ")
    assert ticket

    # Simulate the joined session's hub client having died, exactly as
    # test_session_reaper_sweeps_dead_sessions does in test_mcp_http.py.
    monkeypatch.setattr(state, "client_for", lambda _tok: None)
    assert mcp_http._session_reaper_fn is not None
    mcp_http._session_reaper_fn()

    assert state.redeem_watch_ticket(ticket) is None


def _hub_flag(command: str) -> str:
    """Return the value the emitted command passes to ``caucus-watch --hub``."""
    parts = command.split()
    return parts[parts.index("--hub") + 1]


async def test_watch_command_https_url_is_accepted_by_the_watcher(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emitted --hub must survive the check caucus-watch runs on it."""
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    server = build_mcp_server(
        hub_module.app, self_url="https://hub.example.net", remote=True
    )
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")

    command = str((await _tool(server, "watch_command")(ctx))["command"])
    # https needs no opt-in, so the command must not carry one either.
    assert not command.startswith(ALLOW_REMOTE_ENV)
    assert validate_hub_url(_hub_flag(command)) == "https://hub.example.net"


async def test_watch_command_plain_http_url_carries_the_opt_in(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain-http public URL is refused by the watcher unless the command says so.

    Without the prefix the agent backgrounds a process that exits 2 before its
    first poll and believes a watcher is listening.
    """
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    server = build_mcp_server(
        hub_module.app, self_url="http://hub.lan:8765", remote=True
    )
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")

    command = str((await _tool(server, "watch_command")(ctx))["command"])
    assert command.startswith(f"{ALLOW_REMOTE_ENV}=1 caucus-watch ")
    # Bare, the URL is refused; with the opt-in the command's own prefix sets,
    # it is accepted -- which is the whole point of emitting the prefix.
    with pytest.raises(ValueError):
        validate_hub_url(_hub_flag(command))
    monkeypatch.setenv(ALLOW_REMOTE_ENV, "1")
    assert validate_hub_url(_hub_flag(command)) == "http://hub.lan:8765"


async def test_watch_command_remote_writes_no_token_file(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remote branch must not create a file only the hub can see."""
    from caucus import mcp_http

    def _forbidden(_token: str) -> str:
        raise AssertionError("a remote deployment must not write a token file")

    monkeypatch.setattr(mcp_http, "_write_token_file", _forbidden)
    server = build_mcp_server(
        hub_module.app, self_url="https://hub.example.net", remote=True
    )
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")
    await _tool(server, "watch_command")(ctx)


# ---------------------------------------------------------------------------
# Watch tickets: HubState store and POST /watch-ticket/redeem
# ---------------------------------------------------------------------------

REDEEM_PATH = "/watch-ticket/redeem"


def test_watch_ticket_redeems_exactly_once(state: HubState) -> None:
    """Single use is the property the whole design rests on."""
    ticket = state.issue_watch_ticket("peer-token")
    assert state.redeem_watch_ticket(ticket) == "peer-token"
    assert state.redeem_watch_ticket(ticket) is None


def test_watch_ticket_expires(state: HubState) -> None:
    """Past its TTL a ticket is gone, whether or not anyone swept it.

    The clock is driven, not slept on: a real wait would put two minutes of
    dead time in the suite to prove one comparison.
    """
    ticket = state.issue_watch_ticket("peer-token", now=1000.0)
    assert state.redeem_watch_ticket(ticket, now=1000.0 + WATCH_TICKET_TTL - 1) == (
        "peer-token"
    )
    again = state.issue_watch_ticket("peer-token", now=2000.0)
    assert state.redeem_watch_ticket(again, now=2000.0 + WATCH_TICKET_TTL + 1) is None


def test_unknown_watch_ticket_redeems_to_nothing(state: HubState) -> None:
    """A guessed or invented ticket buys nothing."""
    state.issue_watch_ticket("peer-token")
    assert state.redeem_watch_ticket("not-a-ticket") is None


def test_expired_watch_tickets_are_swept_by_the_idle_reaper(state: HubState) -> None:
    """The store must not grow: the sweep the hub already runs prunes it."""
    state.issue_watch_ticket("peer-token", now=1000.0)
    assert len(state._watch_tickets) == 1
    state.reap_stale(state.client_ttl, now=1000.0 + WATCH_TICKET_TTL + 1)
    assert state._watch_tickets == {}


def test_redeem_endpoint_returns_the_token_once(client: TestClient) -> None:
    """The endpoint is the state store's behaviour over HTTP: 200 then 404."""
    ticket = hub_module.state.issue_watch_ticket("peer-token")
    resp = client.post(REDEEM_PATH, json={"ticket": ticket})
    assert resp.status_code == 200
    assert resp.json() == {"token": "peer-token"}
    replay = client.post(REDEEM_PATH, json={"ticket": ticket})
    assert replay.status_code == 404
    assert "watch_command()" in replay.json()["detail"]


def test_redeem_endpoint_404s_an_unknown_ticket(client: TestClient) -> None:
    """404, not 401: the caller's credential was fine, the ticket is not there."""
    assert client.post(REDEEM_PATH, json={"ticket": "nope"}).status_code == 404


def test_redeem_endpoint_requires_the_agent_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyed hub must not hand peer tokens to whoever can reach the port."""
    ticket = hub_module.state.issue_watch_ticket("peer-token")
    monkeypatch.setattr(hub_module, "auth_config", AuthConfig(agent="the-key"))
    refused = client.post(REDEEM_PATH, json={"ticket": ticket})
    assert refused.status_code == 401
    # The refusal fires before the lookup, so the ticket survives it intact.
    ok = client.post(
        REDEEM_PATH,
        json={"ticket": ticket},
        headers={"Authorization": "Bearer the-key"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"token": "peer-token"}


def test_a_ticket_for_a_dead_peer_resurrects_nothing(client: TestClient) -> None:
    """A ticket is a claim check on a token, not a second life for a peer.

    Redemption knows nothing about clients, so it still answers; the token it
    hands back is then refused by ``client_for`` exactly as it already is
    today, and the watcher's first ``/receive`` earns the usual 401.
    """
    token = client.post("/register", json={"project": "ghost"}).json()["token"]
    ticket = hub_module.state.issue_watch_ticket(token)
    assert hub_module.state.unregister(token) == "ghost"

    resp = client.post(REDEEM_PATH, json={"ticket": ticket})
    assert resp.status_code == 200
    handed_back = resp.json()["token"]
    assert handed_back == token
    assert hub_module.state.client_for(handed_back) is None


# ---------------------------------------------------------------------------
# _mount_mcp_http wiring
# ---------------------------------------------------------------------------


def _capture_build(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``build_mcp_server`` and return the dict its kwargs land in."""
    from caucus import mcp_http

    captured: dict[str, Any] = {}

    def _fake(app: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return type("_Server", (), {"streamable_http_app": lambda self: _Sub()})()

    class _Sub:
        routes: list[Any] = []

    monkeypatch.setattr(mcp_http, "build_mcp_server", _fake)
    monkeypatch.setattr(hub_module, "server_config", ServerConfig())
    # _mount_mcp_http writes these two module globals; restore them so a stub
    # server never outlives this test.
    monkeypatch.setattr(hub_module, "_mcp_server", hub_module._mcp_server)
    monkeypatch.setattr(hub_module, "_session_reaper_fn", hub_module._session_reaper_fn)
    return captured


def test_mount_passes_public_url_as_self_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The advertised URL replaces the 127.0.0.1 rewrite of a wildcard bind."""
    captured = _capture_build(monkeypatch)
    hub_module._mount_mcp_http(
        host="0.0.0.0",
        port=8765,
        mcp_path="/mcp",
        extra_origins=set(),
        extra_hosts=["hub.lan:8765"],
        public_url="https://hub.example.net",
    )
    assert captured["self_url"] == "https://hub.example.net"
    assert captured["remote"] is True
    # The operator's entry, plus the public URL's own netloc added for free.
    assert "hub.lan:8765" in captured["allowed_hosts"]
    assert "hub.example.net" in captured["allowed_hosts"]


def test_mount_without_public_url_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain loopback mount still advertises itself and stays non-remote."""
    captured = _capture_build(monkeypatch)
    hub_module._mount_mcp_http(
        host="127.0.0.1", port=8765, mcp_path="/mcp", extra_origins=set()
    )
    assert captured["self_url"] == "http://127.0.0.1:8765"
    assert captured["remote"] is False
    assert captured["allowed_hosts"] == ["127.0.0.1:8765"]


def test_mount_does_not_repeat_the_bind_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming the bind address as --allowed-host too must not list it twice."""
    captured = _capture_build(monkeypatch)
    hub_module._mount_mcp_http(
        host="192.168.1.10",
        port=8765,
        mcp_path="/mcp",
        extra_origins=set(),
        extra_hosts=["192.168.1.10:8765"],
    )
    assert captured["allowed_hosts"] == ["192.168.1.10:8765"]


def test_mount_on_a_non_loopback_bind_is_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No public URL, but a LAN bind: the watcher command still must travel."""
    captured = _capture_build(monkeypatch)
    hub_module._mount_mcp_http(
        host="192.168.1.10", port=8765, mcp_path="/mcp", extra_origins=set()
    )
    assert captured["remote"] is True


# ---------------------------------------------------------------------------
# The non-loopback bind refusal
# ---------------------------------------------------------------------------


@pytest.fixture
def run_main(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a callable running ``hub.main()`` with the server stubbed out.

    ``uvicorn.run`` is replaced by a recorder, so "the hub starts" is observable
    without binding a socket, and every global ``main`` writes to is restored by
    ``monkeypatch`` when the test ends.
    """
    started: list[tuple[str, int]] = []

    def _run(*_args: Any, **kwargs: Any) -> None:
        started.append((kwargs["host"], kwargs["port"]))

    monkeypatch.setattr(hub_module.uvicorn, "run", _run)
    monkeypatch.setattr(hub_module.coloredlogs, "install", lambda **_kw: None)
    monkeypatch.setattr(hub_module, "auth_config", AuthConfig())
    monkeypatch.setattr(hub_module, "server_config", ServerConfig())

    def _main(*argv: str) -> list[tuple[str, int]]:
        # --no-mcp-http keeps the mount out of the import-time app, which other
        # tests in the session share; --no-browser keeps the timer thread away.
        monkeypatch.setattr(
            sys, "argv", ["caucus-hub", "--no-browser", "--no-mcp-http", *argv]
        )
        hub_module.main()
        return started

    return _main


def test_non_loopback_bind_without_credentials_refuses(
    run_main: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The headline behaviour change: 0.0.0.0 no longer starts silently."""
    with pytest.raises(SystemExit) as excinfo:
        run_main("--host", "0.0.0.0")
    assert excinfo.value.code == 2
    message = capsys.readouterr().err
    # The refusal is the feature: every flag and env var needed to fix it.
    for needle in (
        "--agent-key",
        "CAUCUS_AGENT_KEY",
        "--operator-token",
        "CAUCUS_OPERATOR_TOKEN",
        "--public-url",
        "CAUCUS_PUBLIC_URL",
        "--allowed-host",
        "CAUCUS_ALLOWED_HOSTS",
        "--allow-insecure-bind",
        "--host 127.0.0.1",
    ):
        assert needle in message, f"refusal does not mention {needle}"


def test_refusal_names_which_credential_is_missing(
    run_main: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Half-configured is the common case; say which half is done."""
    with pytest.raises(SystemExit):
        run_main("--host", "0.0.0.0", "--operator-token", "op123")
    message = capsys.readouterr().err
    assert "--operator-token TOKEN  (env CAUCUS_OPERATOR_TOKEN): already set" in message
    assert "--agent-key KEY  (env CAUCUS_AGENT_KEY): MISSING" in message


def test_non_loopback_bind_with_both_credentials_starts(run_main: Any) -> None:
    """Both doors locked and an address to advertise, so the bind is allowed."""
    started = run_main(
        "--host",
        "0.0.0.0",
        "--operator-token",
        "op123",
        "--agent-key",
        "key123",
        "--public-url",
        "https://hub.example.net",
    )
    assert started == [("0.0.0.0", 8765)]


def test_wildcard_bind_without_a_public_url_refuses(
    run_main: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """0.0.0.0 is not an address: with both keys set, the URL is still missing."""
    with pytest.raises(SystemExit) as excinfo:
        run_main(
            "--host", "0.0.0.0", "--operator-token", "op123", "--agent-key", "key123"
        )
    assert excinfo.value.code == 2
    message = capsys.readouterr().err
    assert "--public-url URL    (env CAUCUS_PUBLIC_URL): MISSING" in message
    # The two doors it *has* got are marked done, so the operator reads one gap.
    assert "--agent-key KEY  (env CAUCUS_AGENT_KEY): already set" in message


def test_concrete_non_loopback_bind_needs_no_public_url(run_main: Any) -> None:
    """A real interface address advertises itself, so only the keys are demanded."""
    started = run_main(
        "--host", "192.168.1.10", "--operator-token", "op123", "--agent-key", "key123"
    )
    assert started == [("192.168.1.10", 8765)]


def test_the_whole_loopback_range_skips_the_bind_gate(run_main: Any) -> None:
    """127.0.0.2 is loopback by any honest definition, and now by this one too."""
    started = run_main("--host", "127.0.0.2")
    assert started == [("127.0.0.2", 8765)]


def test_allow_insecure_bind_is_the_escape_hatch(run_main: Any) -> None:
    """An operator who means it can still run wide open, explicitly."""
    started = run_main("--host", "0.0.0.0", "--allow-insecure-bind")
    assert started == [("0.0.0.0", 8765)]


def test_loopback_without_credentials_still_starts(run_main: Any) -> None:
    """The default localhost posture is untouched."""
    started = run_main("--host", "127.0.0.1")
    assert started == [("127.0.0.1", 8765)]


def test_credentials_may_come_from_the_environment(
    run_main: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three flags default from their env vars, so the gate sees them too."""
    monkeypatch.setenv("CAUCUS_OPERATOR_TOKEN", "op123")
    monkeypatch.setenv("CAUCUS_AGENT_KEY", "key123")
    monkeypatch.setenv("CAUCUS_PUBLIC_URL", "https://hub.example.net")
    started = run_main("--host", "0.0.0.0")
    assert started == [("0.0.0.0", 8765)]


def test_plain_http_public_url_warns_at_startup(
    run_main: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Said once, at boot: a plain-http advertised URL leaks every peer token."""
    with caplog.at_level("WARNING", logger="caucus.hub"):
        run_main(
            "--host",
            "0.0.0.0",
            "--operator-token",
            "op123",
            "--agent-key",
            "key123",
            "--public-url",
            "http://hub.lan:8765",
        )
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "in clear" in warning
    assert "CAUCUS_ALLOW_REMOTE_HUB" in warning


def test_https_public_url_does_not_warn(
    run_main: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning is about cleartext, so TLS must silence it."""
    with caplog.at_level("WARNING", logger="caucus.hub"):
        run_main(
            "--host",
            "0.0.0.0",
            "--operator-token",
            "op123",
            "--agent-key",
            "key123",
            "--public-url",
            "https://hub.example.net",
        )
    assert "in clear" not in "\n".join(r.getMessage() for r in caplog.records)


def test_blank_credentials_are_normalised_to_none(run_main: Any) -> None:
    """An empty credential means "none configured", not one nobody can present.

    Left raw, ``--agent-key ""`` makes ``agent_ok`` reject every caller and
    ``--operator-token ""`` flips ``AuthConfig.enabled`` on with a token no
    first frame can match -- a lockout at both doors. The clients already
    normalise blank to ``None``; the hub was the odd one out.
    """
    run_main(
        "--agent-key", "", "--operator-token", "", "--observer-token", ""
    )
    assert hub_module.auth_config.agent is None
    assert hub_module.auth_config.operator is None
    assert hub_module.auth_config.observer is None
    assert hub_module.auth_config.agent_ok(None) is True
    assert hub_module.auth_config.enabled is False


def test_a_blank_public_url_is_not_an_advertised_address(run_main: Any) -> None:
    """``--public-url ""`` already normalised to None; keep it that way."""
    started = run_main("--public-url", "")
    assert started == [("127.0.0.1", 8765)]


def test_an_invalid_public_url_refuses_at_startup(
    run_main: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A public URL with a path would build unreachable addresses; refuse it."""
    with pytest.raises(SystemExit):
        run_main("--public-url", "https://hub.example.net/caucus")
    assert "bare origin" in capsys.readouterr().err
