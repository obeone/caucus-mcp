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
from caucus.models import BROADCAST, MAX_PATH_CHARS, Message
from caucus.state import HubState
from caucus.supervisor import MAX_CWD_COMPLETIONS, AgentSupervisor, LauncherConfig

OPERATOR_TOKEN = "op-secret"
OBSERVER_TOKEN = "obs-secret"


class _StubProcess:
    """Stand-in for an :mod:`asyncio` process handle that owns no process.

    It models a child that stays alive until something signals it, because the
    supervisor now watches every child with a per-record exit waiter: a handle
    whose ``wait`` returned straight away would be marked exited the instant it
    was spawned, and no endpoint test could ever kill one.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = None
        self.stderr = None
        self._exited = asyncio.Event()

    def expire(self) -> None:
        """Mark the fabricated child dead and release everyone waiting on it."""
        if self.returncode is None:
            self.returncode = 0
        self._exited.set()

    async def wait(self) -> int:
        """Block until :meth:`expire` is called, then report a clean exit."""
        await self._exited.wait()
        return 0


class _FakeSupervisor(AgentSupervisor):
    """Supervisor that fabricates children instead of forking them.

    Overrides the two methods that touch the operating system, so the endpoint
    tests exercise routing, gating and rendering with no process anywhere.

    Attributes
    ----------
    signals:
        One entry per signal that would have been sent.
    spawn_cwds:
        The working directory each fabricated child would have started in. The
        per-spawn ``cwd`` tests read this: the spec carrying a path proves
        nothing on its own, the directory handed to the launch step does.
    """

    signals: list[tuple[str, int]]
    spawn_cwds: list[Path]

    def __init__(self, config: LauncherConfig, hub_url: str) -> None:
        super().__init__(
            config,
            hub_url,
            hub_module._broadcast_agents,
            # Mirrors what the hub's lifespan wires up: read-time probes into
            # the live HubState, resolved through the module global so the
            # per-test state swap is honoured.
            peer_exists=lambda name: hub_module.state.peer_info(name) is not None,
            peer_msg_count=hub_module._peer_msg_count,
        )
        self.signals = []
        self.spawn_cwds = []
        self._next_pid = 30000

    async def _spawn_process(
        self, argv: list[str], env: dict[str, str], cwd: Path
    ) -> asyncio.subprocess.Process:
        """Return a stub handle rather than starting anything."""
        self._next_pid += 1
        self.spawn_cwds.append(cwd)
        return _StubProcess(self._next_pid)  # type: ignore[return-value]

    def _signal_group(self, record: object, sig: object) -> None:  # type: ignore[override]
        """Record the signal, then let the fabricated child die from it."""
        self.signals.append((getattr(record, "pid", 0), int(sig)))  # type: ignore[arg-type]
        process = getattr(record, "process", None)
        if isinstance(process, _StubProcess):
            process.expire()


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
    assert listed[0]["msg_count"] is None

    killed = client.delete("/agents/alpha")
    assert killed.status_code == 200
    assert killed.json() == {"name": "alpha", "killed": True}
    assert launcher.signals  # a signal was aimed at the group, not the child


def test_spawn_response_hides_cwd_and_output(
    client: TestClient, launcher: _FakeSupervisor, workdir: Path
) -> None:
    """A spawn reply never leaks the working directory or child output."""
    body = client.post("/agents", json={"name": "alpha"}).json()["agent"]
    assert "stdout" not in body
    assert "stderr" not in body
    assert "cwd" not in body
    assert str(workdir) not in resp_text(body)


def resp_text(payload: object) -> str:
    """Render a payload as text, for substring assertions."""
    return repr(payload)


def test_get_agents_serves_both_output_streams_to_the_operator(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """The operator-gated listing is the one place child output is exposed.

    Both streams are served, under separate keys: a wedged child's own account of
    itself lands on stdout, and merging it into the diagnostics would lose it.
    """
    client.post("/agents", json={"name": "alpha"})
    record = launcher.get("alpha")
    assert record is not None
    record.stdout_tail.append("waiting for approval")
    record.stderr_tail.append("traceback line")
    listed = client.get("/agents").json()["agents"]
    assert listed[0]["stdout"] == ["waiting for approval"]
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


# --- per-spawn working directory ---------------------------------------------


def _bad_cwd(kind: str, tmp_path: Path) -> str:
    """Build one invalid working-directory value of the requested shape.

    Parameters
    ----------
    kind:
        ``relative``, ``nul``, ``dotdot``, ``missing``, ``file``, or
        ``symlink``.
    tmp_path:
        Scratch directory to build inside.

    Returns
    -------
    str
        The value to put in the spawn body's ``cwd``.
    """
    if kind == "relative":
        return "relative/dir"
    if kind == "nul":
        # Every path syscall raises ValueError (not OSError) on an embedded NUL,
        # so without an explicit rule this escapes as a 500 rather than a 400.
        return f"{tmp_path}/a\x00b"
    if kind == "dotdot":
        return str(tmp_path / ".." / tmp_path.name)
    if kind == "missing":
        return str(tmp_path / "ghost")
    if kind == "file":
        target = tmp_path / "file.txt"
        target.write_text("x")
        return str(target.resolve())
    if kind == "symlink":
        real = (tmp_path / "real").resolve()
        real.mkdir()
        link = (tmp_path / "link").resolve()
        link.symlink_to(real)
        return str(link)
    raise AssertionError(f"unknown kind {kind!r}")


def test_per_spawn_cwd_is_honoured(
    client: TestClient, launcher: _FakeSupervisor, tmp_path: Path, workdir: Path
) -> None:
    """The directory the operator chose is the one the child starts in."""
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    resp = client.post("/agents", json={"name": "alpha", "cwd": str(elsewhere)})
    assert resp.status_code == 200
    assert launcher.spawn_cwds == [elsewhere]
    assert workdir not in launcher.spawn_cwds


@pytest.mark.parametrize("body", [{}, {"cwd": None}, {"cwd": ""}])
def test_absent_null_or_empty_cwd_uses_the_hub_default(
    client: TestClient,
    launcher: _FakeSupervisor,
    workdir: Path,
    body: dict[str, object],
) -> None:
    """Nothing chosen means the hub's configured default, not a refusal."""
    resp = client.post("/agents", json={"name": "alpha", **body})
    assert resp.status_code == 200
    assert launcher.spawn_cwds == [workdir]


@pytest.mark.parametrize(
    "kind", ["relative", "nul", "dotdot", "missing", "file", "symlink"]
)
def test_invalid_per_spawn_cwd_is_400(
    client: TestClient, launcher: _FakeSupervisor, tmp_path: Path, kind: str
) -> None:
    """Every path rule is enforced server-side, before any process exists.

    A 400 for each, never a 500: a refusal the operator can read is the whole
    point, and ``nul`` is the shape that used to escape as a server error.
    """
    resp = client.post(
        "/agents", json={"name": "alpha", "cwd": _bad_cwd(kind, tmp_path)}
    )
    assert resp.status_code == 400
    assert "working directory" in resp.json()["detail"]
    assert launcher.list() == []
    assert launcher.spawn_cwds == []


def test_oversized_cwd_is_rejected_before_the_filesystem(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """``cwd`` is bounded like every other string on the spawn body.

    The 422 comes from the model, so the value never reaches a syscall. An
    oversized path would also degrade cleanly below (``ENAMETOOLONG`` arrives as
    an ``OSError`` and becomes a refusal), but the class docstring promises the
    bounds keep an oversized body from reaching the supervisor at all.
    """
    resp = client.post(
        "/agents", json={"name": "alpha", "cwd": "/" + "x" * MAX_PATH_CHARS}
    )
    assert resp.status_code == 422
    assert launcher.list() == []
    assert launcher.spawn_cwds == []


def test_oversized_completion_prefix_is_rejected(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """The completion prefix carries the same bound as the spawn body's path."""
    resp = client.get(
        "/agents/cwd-complete", params={"prefix": "/" + "x" * MAX_PATH_CHARS}
    )
    assert resp.status_code == 422


def test_cwd_at_the_length_bound_still_reaches_validation(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """The bound refuses only what is over it, not what sits exactly on it.

    A path of exactly ``MAX_PATH_CHARS`` passes the model and is refused by
    ``validate_agent_cwd`` instead (it does not exist), which is a 400: proof the
    value got through the bound rather than being stopped by it.
    """
    resp = client.post(
        "/agents", json={"name": "alpha", "cwd": "/" + "x" * (MAX_PATH_CHARS - 1)}
    )
    assert resp.status_code == 400
    assert "working directory" in resp.json()["detail"]


def test_spawn_response_still_hides_the_chosen_cwd(
    client: TestClient, launcher: _FakeSupervisor, tmp_path: Path
) -> None:
    """Choosing the directory per spawn did not put it in the roster row."""
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    row = client.post(
        "/agents", json={"name": "alpha", "cwd": str(elsewhere)}
    ).json()["agent"]
    assert "cwd" not in row
    assert str(elsewhere) not in resp_text(row)
    listed = client.get("/agents").json()["agents"][0]
    assert "cwd" not in listed
    assert str(elsewhere) not in resp_text(listed)


def test_unknown_body_field_is_rejected(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """A field this hub does not implement fails loudly instead of silently.

    ``extra_args`` is the case that matters: free argv is the one thing the
    spawn body still deliberately refuses, so a console sending it must not
    believe it passed flags the supervisor never reviewed. (``cwd`` used to
    stand here; it is now a real, validated field.)
    """
    resp = client.post(
        "/agents", json={"name": "alpha", "extra_args": ["--dangerously-skip"]}
    )
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


def test_roster_tells_a_phantom_from_a_talking_agent(
    client: TestClient, launcher: _FakeSupervisor, state: HubState
) -> None:
    """``msg_count`` separates an agent that joined from one that speaks.

    A wedged child keeps its peer's ``last_seen`` fresh by long-polling, so the
    row reads healthy on every other field. The send count is the one that moves
    only when the agent actually says something.
    """
    client.post("/agents", json={"name": "alpha"})
    state.register("alpha")

    joined = client.get("/agents").json()["agents"][0]
    assert joined["state"] == "running"
    assert joined["peer_known"] is True
    assert joined["msg_count"] == 0  # a phantom: present, healthy, silent

    state.route(Message(sender="alpha", recipient=BROADCAST, content="hello"))
    spoke = client.get("/agents").json()["agents"][0]
    assert spoke["msg_count"] == 1


# --- console fan-out ---------------------------------------------------------


def test_observer_receives_the_roster_without_output(
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
    assert all("stdout" not in row for row in event["agents"])
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
    assert all("stdout" not in row for row in snapshot["agents"])
    assert all("stderr" not in row for row in snapshot["agents"])


def test_snapshot_carries_the_default_cwd_for_an_operator(
    client: TestClient, launcher: _FakeSupervisor, workdir: Path, auth_on: None
) -> None:
    """The spawn form's pre-fill reaches the operator who may use it."""
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": OPERATOR_TOKEN})
        assert ws.receive_json()["type"] == "auth_ok"
        snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert snapshot["agent_cwd"] == str(workdir)


def test_snapshot_hides_the_default_cwd_from_an_observer(
    client: TestClient, launcher: _FakeSupervisor, workdir: Path, auth_on: None
) -> None:
    """An observer never learns a filesystem path on the operator's machine.

    Same reason the roster omits the cwd: an observer may watch the room, not
    read the hub's own filesystem layout. They cannot spawn anything, so the
    pre-fill would be useless to them as well as none of their business.
    """
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": OBSERVER_TOKEN})
        assert ws.receive_json()["type"] == "auth_ok"
        snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert "agent_cwd" not in snapshot
    assert str(workdir) not in resp_text(snapshot)


def test_snapshot_omits_the_default_cwd_when_the_launcher_is_off(
    client: TestClient,
) -> None:
    """No launcher means no key at all, so a console can tell the two apart."""
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert "agent_cwd" not in snapshot


# --- working directory completion --------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small directory tree for the completion endpoint to walk.

    Two matching subdirectories, one non-matching, a matching regular file that
    must never appear, a hidden directory, and a nested child that must not
    either, since completion does not recurse.
    """
    root = (tmp_path / "tree").resolve()
    (root / "alpha" / "nested").mkdir(parents=True)
    (root / "alphabet").mkdir()
    (root / "beta").mkdir()
    (root / ".hidden").mkdir()
    (root / "alpha-file.txt").write_text("x")
    return root


def test_cwd_complete_lists_only_directories(
    client: TestClient, launcher: _FakeSupervisor, tree: Path
) -> None:
    """Directory names only: never a file, never a file's contents."""
    body = client.get("/agents/cwd-complete", params={"prefix": f"{tree}/"}).json()
    assert body == {
        "dirs": [
            str(tree / "alpha"),
            str(tree / "alphabet"),
            str(tree / "beta"),
        ],
        "truncated": False,
    }


def test_cwd_complete_skips_symlinked_directories(
    client: TestClient, launcher: _FakeSupervisor, tmp_path: Path
) -> None:
    """Every candidate is a path the spawn would accept, symlinks excluded.

    A ``current``-style symlink is the common case, and offering it would mean
    the operator picks a suggestion and gets a 400 back.
    """
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "release-1").mkdir()
    (root / "current").symlink_to(root / "release-1")
    dirs = client.get(
        "/agents/cwd-complete", params={"prefix": f"{root}/"}
    ).json()["dirs"]
    assert dirs == [str(root / "release-1")]
    # The offered candidate really is spawnable, which is the point.
    spawned = client.post("/agents", json={"name": "alpha", "cwd": dirs[0]})
    assert spawned.status_code == 200
    # The symlink the dropdown withheld is exactly what the spawn refuses.
    refused = client.post(
        "/agents", json={"name": "beta", "cwd": str(root / "current")}
    )
    assert refused.status_code == 400
    assert "resolves elsewhere" in refused.json()["detail"]


def test_cwd_complete_filters_on_the_partial_segment(
    client: TestClient, launcher: _FakeSupervisor, tree: Path
) -> None:
    """The half-typed last segment narrows the list, without recursing."""
    body = client.get(
        "/agents/cwd-complete", params={"prefix": str(tree / "alph")}
    ).json()
    assert body["dirs"] == [str(tree / "alpha"), str(tree / "alphabet")]
    assert str(tree / "alpha" / "nested") not in body["dirs"]


def test_cwd_complete_hides_dotted_entries_until_asked(
    client: TestClient, launcher: _FakeSupervisor, tree: Path
) -> None:
    """A hidden directory shows up only once the operator types the dot."""
    visible = client.get(
        "/agents/cwd-complete", params={"prefix": f"{tree}/"}
    ).json()["dirs"]
    assert str(tree / ".hidden") not in visible
    asked = client.get(
        "/agents/cwd-complete", params={"prefix": f"{tree}/."}
    ).json()["dirs"]
    assert asked == [str(tree / ".hidden")]


def test_cwd_complete_caps_and_flags_truncation(
    client: TestClient, launcher: _FakeSupervisor, tmp_path: Path
) -> None:
    """One keystroke on a huge parent cannot return a huge payload."""
    root = (tmp_path / "many").resolve()
    root.mkdir()
    for index in range(MAX_CWD_COMPLETIONS + 3):
        (root / f"dir{index:03d}").mkdir()
    body = client.get("/agents/cwd-complete", params={"prefix": f"{root}/"}).json()
    assert len(body["dirs"]) == MAX_CWD_COMPLETIONS
    assert body["truncated"] is True


def test_cwd_complete_is_empty_for_a_missing_parent(
    client: TestClient, launcher: _FakeSupervisor, tmp_path: Path
) -> None:
    """A path mid-typing is not an error, just an empty list."""
    resp = client.get(
        "/agents/cwd-complete", params={"prefix": str(tmp_path / "ghost" / "part")}
    )
    assert resp.status_code == 200
    assert resp.json() == {"dirs": [], "truncated": False}


def test_cwd_complete_defaults_to_the_root(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """A missing prefix behaves as ``/`` rather than failing.

    The non-emptiness assertion is load-bearing: ``all(...)`` over an empty list
    is ``True``, so without it a regression that returned nothing for an empty
    partial segment would keep this test green.
    """
    resp = client.get("/agents/cwd-complete")
    assert resp.status_code == 200
    dirs = resp.json()["dirs"]
    assert dirs, "the filesystem root must yield at least one candidate"
    assert all(item.startswith("/") for item in dirs)


def test_cwd_complete_refuses_a_relative_prefix(
    client: TestClient, launcher: _FakeSupervisor
) -> None:
    """A relative prefix is a 400: the hub's cwd is not the operator's."""
    resp = client.get("/agents/cwd-complete", params={"prefix": "relative/dir"})
    assert resp.status_code == 400
    assert "absolute" in resp.json()["detail"]


def test_cwd_complete_refuses_a_nul_byte(
    client: TestClient, launcher: _FakeSupervisor, tmp_path: Path
) -> None:
    """A NUL in the prefix is a 400, not a 500 out of ``os.scandir``.

    The NUL sits in the *parent* segment, which is the half of the prefix that
    reaches a syscall; ``scandir`` raises ``ValueError`` on it, and that is not
    an ``OSError``, so the empty-list guard never sees it.
    """
    resp = client.get(
        "/agents/cwd-complete", params={"prefix": f"{tmp_path}/a\x00b/"}
    )
    assert resp.status_code == 400
    assert "NUL byte" in resp.json()["detail"]


def test_cwd_complete_403_when_launcher_disabled(client: TestClient) -> None:
    """A hub that cannot spawn anything does not answer path questions."""
    assert client.get("/agents/cwd-complete", params={"prefix": "/"}).status_code == 403


def test_cwd_complete_requires_the_operator_token(
    client: TestClient, launcher: _FakeSupervisor, auth_on: None
) -> None:
    """Missing and observer tokens are both refused, like every /agents route."""
    params = {"prefix": "/"}
    assert client.get("/agents/cwd-complete", params=params).status_code == 401
    assert (
        client.get(
            "/agents/cwd-complete", params=params, headers=_bearer(OBSERVER_TOKEN)
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/agents/cwd-complete", params=params, headers=_bearer(OPERATOR_TOKEN)
        ).status_code
        == 200
    )


def test_cwd_complete_refuses_a_disallowed_origin(
    client: TestClient, launcher: _FakeSupervisor, auth_on: None
) -> None:
    """The CSRF gate applies here too, before anything is listed."""
    headers = {**_bearer(OPERATOR_TOKEN), "Origin": "https://evil.example"}
    resp = client.get(
        "/agents/cwd-complete", params={"prefix": "/"}, headers=headers
    )
    assert resp.status_code == 403


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
