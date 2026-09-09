"""Tests for the hub's ``/agents`` endpoints and the launcher's boot gate.

Nothing here starts a real process: the supervisor installed on the hub module
is a subclass whose launch step returns a stub handle and whose signal step is a
no-op recorder, so an API test can never signal a real process group or leave a
child behind. Nothing here talks to a hub on ``127.0.0.1:8765`` either.

The boot-gate tests are the important ones. Hub auth is off by default and
``AuthConfig.role_for`` grades everyone as ``operator`` in that state, so
"require the operator token" gates nothing unless the hub refuses to enable the
launcher without one in the first place.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from caucus import hub as hub_module
from caucus.state import HubState
from caucus.supervisor import AgentSupervisor, LauncherConfig

OPERATOR_TOKEN = "op-secret"
OBSERVER_TOKEN = "obs-secret"


class _StubProcess:
    """Stand-in for an :mod:`asyncio` process handle that owns no process."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stderr = None

    async def wait(self) -> int:
        """Report an immediate clean exit."""
        self.returncode = 0
        return 0


class _FakeSupervisor(AgentSupervisor):
    """Supervisor that fabricates children instead of forking them.

    Overrides the two methods that touch the operating system, so the endpoint
    tests exercise routing, gating and rendering with no process anywhere.
    """

    signals: list[tuple[str, int]]

    def __init__(self, config: LauncherConfig, hub_url: str) -> None:
        super().__init__(
            config,
            hub_url,
            hub_module._broadcast_agents,
            # Mirrors what the hub's lifespan wires up: a read-time probe into
            # the live HubState, resolved through the module global so the
            # per-test state swap is honoured.
            peer_exists=lambda name: hub_module.state.peer_info(name) is not None,
        )
        self.signals = []
        self._next_pid = 30000

    async def _spawn_process(
        self, argv: list[str], env: dict[str, str], cwd: Path
    ) -> asyncio.subprocess.Process:
        """Return a stub handle rather than starting anything."""
        self._next_pid += 1
        return _StubProcess(self._next_pid)  # type: ignore[return-value]

    def _signal_group(self, record: object, sig: object) -> None:  # type: ignore[override]
        """Record the signal instead of delivering it to a process group."""
        self.signals.append((getattr(record, "pid", 0), int(sig)))  # type: ignore[arg-type]


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A resolved, existing directory to configure as the launcher cwd."""
    work = (tmp_path / "agents").resolve()
    work.mkdir()
    return work


@pytest.fixture
def launcher(
    client: TestClient, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_FakeSupervisor]:
    """Install an enabled, process-free supervisor on the live hub module."""
    sup = _FakeSupervisor(
        LauncherConfig(enabled=True, cwd=workdir, max_agents=3),
        "http://127.0.0.1:9/",
    )
    monkeypatch.setattr(hub_module, "supervisor", sup)
    yield sup


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn hub auth on with a distinct operator and observer token."""
    monkeypatch.setattr(hub_module.auth_config, "operator", OPERATOR_TOKEN)
    monkeypatch.setattr(hub_module.auth_config, "observer", OBSERVER_TOKEN)


def _bearer(token: str) -> dict[str, str]:
    """Build an ``Authorization`` header for ``token``."""
    return {"Authorization": f"Bearer {token}"}


# --- launcher disabled -------------------------------------------------------


def test_get_agents_403_when_launcher_disabled(client: TestClient) -> None:
    """Listing is refused on a hub that never enabled the launcher."""
    assert client.get("/agents").status_code == 403


def test_post_agents_403_when_launcher_disabled(client: TestClient) -> None:
    """Spawning is refused on a hub that never enabled the launcher."""
    resp = client.post("/agents", json={"name": "alpha"})
    assert resp.status_code == 403


def test_delete_agents_403_when_launcher_disabled(client: TestClient) -> None:
    """Killing is refused on a hub that never enabled the launcher."""
    assert client.delete("/agents/alpha").status_code == 403


def test_snapshot_has_no_agents_when_launcher_disabled(client: TestClient) -> None:
    """The console still gets an (empty) roster field, never a missing key."""
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert snapshot["agents"] == []


# --- operator gate -----------------------------------------------------------


def test_agents_require_operator_token(
    client: TestClient, launcher: _FakeSupervisor, auth_on: None
) -> None:
    """Without the operator token, all three endpoints refuse."""
    assert client.get("/agents").status_code == 401
    assert client.post("/agents", json={"name": "alpha"}).status_code == 401
    assert client.delete("/agents/alpha").status_code == 401


def test_agents_refuse_the_observer_token(
    client: TestClient, launcher: _FakeSupervisor, auth_on: None
) -> None:
    """A read-only observer may watch the room but never start a process."""
    headers = _bearer(OBSERVER_TOKEN)
    assert client.get("/agents", headers=headers).status_code == 401
    assert (
        client.post("/agents", json={"name": "alpha"}, headers=headers).status_code
        == 401
    )
    assert client.delete("/agents/alpha", headers=headers).status_code == 401
    assert launcher.list() == []


def test_agents_accept_the_operator_token(
    client: TestClient, launcher: _FakeSupervisor, auth_on: None
) -> None:
    """The operator token opens the endpoints."""
    resp = client.get("/agents", headers=_bearer(OPERATOR_TOKEN))
    assert resp.status_code == 200
    assert resp.json() == {"agents": []}


def test_agents_refuse_a_disallowed_origin(
    client: TestClient, launcher: _FakeSupervisor, auth_on: None
) -> None:
    """A cross-site browser Origin is refused before anything is applied."""
    headers = {**_bearer(OPERATOR_TOKEN), "Origin": "https://evil.example"}
    assert client.get("/agents", headers=headers).status_code == 403
    assert (
        client.post("/agents", json={"name": "alpha"}, headers=headers).status_code
        == 403
    )
    assert client.delete("/agents/alpha", headers=headers).status_code == 403
    assert launcher.list() == []


# --- spawn / list / kill -----------------------------------------------------


def test_spawn_then_list_then_kill(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """The happy path: a spawn shows up in the roster and a kill retires it."""
    resp = client.post(
        "/agents",
        json={"name": "alpha", "mission": "negotiate the API", "type": "talker"},
    )
    assert resp.status_code == 200
    row = resp.json()["agent"]
    assert row["name"] == "alpha"
    assert row["state"] == "running"

    listed = client.get("/agents").json()["agents"]
    assert [item["name"] for item in listed] == ["alpha"]
    assert listed[0]["peer_known"] is False

    killed = client.delete("/agents/alpha")
    assert killed.status_code == 200
    assert killed.json() == {"name": "alpha", "killed": True}
    assert launcher.signals  # a signal was aimed at the group, not the child


def test_spawn_response_hides_cwd_and_stderr(
    client: TestClient, launcher: _FakeSupervisor, workdir: Path
) -> None:
    """A spawn reply never leaks the working directory or child output."""
    body = client.post("/agents", json={"name": "alpha"}).json()["agent"]
    assert "stderr" not in body
    assert "cwd" not in body
    assert str(workdir) not in resp_text(body)


def resp_text(payload: object) -> str:
    """Render a payload as text, for substring assertions."""
    return repr(payload)


def test_get_agents_serves_stderr_to_the_operator(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """The operator-gated listing is the one place stderr is exposed."""
    client.post("/agents", json={"name": "alpha"})
    record = launcher.get("alpha")
    assert record is not None
    record.stderr_tail.append("traceback line")
    listed = client.get("/agents").json()["agents"]
    assert listed[0]["stderr"] == ["traceback line"]


def test_worker_with_bypass_permissions_is_400(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """A refusal from the supervisor becomes a clean 400, not a 500."""
    resp = client.post(
        "/agents",
        json={
            "name": "alpha",
            "type": "worker",
            "permission_mode": "bypassPermissions",
        },
    )
    assert resp.status_code == 400
    assert "bypassPermissions" in resp.json()["detail"]
    assert launcher.list() == []


def test_unknown_body_field_is_rejected(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """A field this hub does not implement fails loudly instead of silently.

    ``cwd`` is the case that matters: a console sending one must not believe it
    chose the working directory.
    """
    resp = client.post("/agents", json={"name": "alpha", "cwd": "/etc"})
    assert resp.status_code == 422
    assert launcher.list() == []


@pytest.mark.parametrize("name", ["%2e%2e", "-dash", "a b", "a" * 65])
def test_delete_rejects_a_malformed_name(
    client: TestClient, launcher: _FakeSupervisor, name: str
) -> None:
    """A traversal-flavoured path segment never matches a record."""
    assert client.delete(f"/agents/{name}").status_code == 404


def test_delete_with_a_literal_dotdot_never_reaches_a_record(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """A raw ``..`` segment is collapsed by the client before it is sent.

    It therefore lands on ``/agents``, which has no DELETE handler, and comes
    back 405. Either way no record is matched; the percent-encoded form, which
    does survive to the handler, is covered above.
    """
    assert client.delete("/agents/..").status_code in (404, 405)


def test_delete_unknown_name_is_404(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """Killing an agent this hub never launched is a 404."""
    assert client.delete("/agents/ghost").status_code == 404


def test_roster_annotates_a_known_peer(
    client: TestClient, launcher: _FakeSupervisor, state: HubState
) -> None:
    """``peer_known`` reflects hub state without writing anything into it."""
    client.post("/agents", json={"name": "alpha"})
    before = client.get("/agents").json()["agents"][0]
    assert before["peer_known"] is False

    state.register("alpha")
    after = client.get("/agents").json()["agents"][0]
    assert after["peer_known"] is True
    # Reading the roster left the room untouched beyond that registration.
    assert state.peer_info("alpha") is not None
    assert len(state.peers_info()) == 1


# --- console fan-out ---------------------------------------------------------


def test_observer_receives_the_roster_without_stderr(
    client: TestClient, launcher: _FakeSupervisor, auth_on: None
) -> None:
    """The broadcast roster reaches observers and carries no child output."""
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": OBSERVER_TOKEN})
        assert ws.receive_json()["type"] == "auth_ok"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["agents"] == []

        client.post(
            "/agents", json={"name": "alpha"}, headers=_bearer(OPERATOR_TOKEN)
        )
        event = _next_event(ws, "agents")

    assert [row["name"] for row in event["agents"]] == ["alpha"]
    assert all("stderr" not in row for row in event["agents"])


def test_snapshot_carries_the_running_roster(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """A console connecting mid-flight is primed with the agents already up."""
    client.post("/agents", json={"name": "alpha"})
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        snapshot = ws.receive_json()
    assert [row["name"] for row in snapshot["agents"]] == ["alpha"]
    assert all("stderr" not in row for row in snapshot["agents"])


def test_ui_socket_has_no_spawn_command(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """HTTP is the launcher's only mutation surface; the socket cannot spawn."""
    assert "spawn_agent" not in hub_module._MUTATING_COMMANDS
    assert "kill_agent" not in hub_module._MUTATING_COMMANDS
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        ws.receive_json()  # snapshot
        ws.send_json({"spawn_agent": {"name": "alpha"}})
        ws.send_json({"heartbeat": "alpha"})
        # The heartbeat reply proves the socket processed both frames; the
        # spawn_agent frame was simply not a command.
        assert ws.receive_json()["type"] == "heartbeat_result"
    assert launcher.list() == []


def _next_event(ws: object, kind: str, limit: int = 10) -> dict[str, object]:
    """Read frames until one of type ``kind`` arrives.

    Parameters
    ----------
    ws:
        An open test WebSocket.
    kind:
        The ``type`` value to wait for.
    limit:
        Most frames to read before giving up.

    Returns
    -------
    dict
        The matching frame.
    """
    for _ in range(limit):
        frame = ws.receive_json()  # type: ignore[attr-defined]
        if frame.get("type") == kind:
            return dict(frame)
    raise AssertionError(f"no {kind!r} event arrived")


# --- boot gate ---------------------------------------------------------------


@pytest.fixture
def boot_env(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> Path:
    """Neutralise the side effects of ``main()`` so the gate can be tested."""
    monkeypatch.setattr(hub_module, "launcher_config", LauncherConfig())
    monkeypatch.setattr(hub_module.auth_config, "operator", None)
    monkeypatch.setattr(hub_module.auth_config, "observer", None)
    monkeypatch.setattr(hub_module, "_open_browser", lambda *a, **k: None)
    monkeypatch.setattr(hub_module.uvicorn, "run", lambda *a, **k: None)
    for var in ("CAUCUS_AGENT_CWD", "CAUCUS_AGENT_MAX", "CAUCUS_OPERATOR_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return workdir


def _run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """Invoke ``hub.main()`` with a synthetic command line."""
    monkeypatch.setattr("sys.argv", ["caucus-hub", "--no-browser", *argv])
    hub_module.main()


def test_launcher_refuses_to_boot_without_an_operator_token(
    monkeypatch: pytest.MonkeyPatch, boot_env: Path
) -> None:
    """Enabling the launcher without an operator token stops the hub dead.

    This is the whole boot gate: with auth off, ``role_for`` grades every caller
    as operator, so the per-request check would let anyone who can reach the
    port start processes on this machine.
    """
    with pytest.raises(SystemExit):
        _run_main(
            monkeypatch,
            "--enable-agent-launcher",
            f"--agent-cwd={boot_env}",
        )
    assert hub_module.launcher_config.enabled is False


def test_launcher_refuses_to_boot_on_a_non_loopback_bind(
    monkeypatch: pytest.MonkeyPatch, boot_env: Path
) -> None:
    """Process creation must not be reachable from the network."""
    with pytest.raises(SystemExit):
        _run_main(
            monkeypatch,
            "--enable-agent-launcher",
            "--operator-token=op",
            "--host=0.0.0.0",
            f"--agent-cwd={boot_env}",
        )
    assert hub_module.launcher_config.enabled is False


def test_launcher_refuses_to_boot_without_a_working_directory(
    monkeypatch: pytest.MonkeyPatch, boot_env: Path
) -> None:
    """The working directory is mandatory, never silently defaulted."""
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, "--enable-agent-launcher", "--operator-token=op")
    assert hub_module.launcher_config.enabled is False


def test_launcher_refuses_a_relative_working_directory(
    monkeypatch: pytest.MonkeyPatch, boot_env: Path
) -> None:
    """A relative ``--agent-cwd`` is refused at boot, not at spawn time."""
    with pytest.raises(SystemExit):
        _run_main(
            monkeypatch,
            "--enable-agent-launcher",
            "--operator-token=op",
            "--agent-cwd=relative/dir",
        )
    assert hub_module.launcher_config.enabled is False


def test_launcher_boots_with_the_full_set(
    monkeypatch: pytest.MonkeyPatch, boot_env: Path
) -> None:
    """Flag plus operator token plus loopback plus a valid cwd: it starts."""
    _run_main(
        monkeypatch,
        "--enable-agent-launcher",
        "--operator-token=op",
        f"--agent-cwd={boot_env}",
        "--agent-max=3",
        "--no-mcp-http",
    )
    assert hub_module.launcher_config.enabled is True
    assert hub_module.launcher_config.cwd == boot_env
    assert hub_module.launcher_config.max_agents == 3


def test_launcher_stays_off_by_default(
    monkeypatch: pytest.MonkeyPatch, boot_env: Path
) -> None:
    """A hub started without the flag has no launcher at all."""
    _run_main(monkeypatch, "--no-mcp-http")
    assert hub_module.launcher_config.enabled is False
