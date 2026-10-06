"""Exercise OpenAI history, cancellation, approvals, and tool profiles offline."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from caucus import openai_agent


async def test_session_builds_full_worker_and_retains_mission_after_reset(
    monkeypatch, state, tmp_path
):
    import httpx

    from caucus import hub
    from caucus.hub_connector import HubConnector

    monkeypatch.setattr(
        openai_agent,
        "HubConnector",
        lambda url: HubConnector(url, transport=httpx.ASGITransport(app=hub.app)),
    )

    async def loop(factory, connector, token, **kwargs):
        assert kwargs["drain_on_stop"] is False
        for _ in range(2):
            async with factory() as client:
                assert not client.history
                assert "Retain this mission" in client.agent.instructions
                assert "native OpenAI connector" in client.agent.instructions
                assert {"say", "run_shell", "delegate_task", "web_search"} <= {
                    t.name for t in client.agent.tools
                }

    monkeypatch.setattr(openai_agent, "_run_loop", loop)
    await openai_agent.run_session(
        hub_url="http://test",
        project="worker",
        mission="Retain this mission",
        model=None,
        poll_timeout=1,
        agent_type="worker",
        cwd=tmp_path,
    )
    assert "worker" not in state._clients


async def test_malformed_room_tool_call_returns_error_instead_of_killing_agent():
    tools = openai_agent.build_tools(ApprovalConnector(), "tok", "talker", None)
    say = next(t for t in tools if t.name == "say")
    assert "tool failed" in await say.on_invoke_tool(None, "not-json")


async def test_real_runner_executes_tool_only_after_hub_operator_answer(
    state, tmp_path
):
    """Run the actual SDK tool loop against the hub, using a scripted model."""
    import httpx
    from agents import Agent, RunConfig, Runner, Usage
    from agents.models.interface import Model, ModelResponse
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from caucus import hub
    from caucus.hub_connector import HubConnector

    class ScriptedModel(Model):
        def __init__(self):
            self.calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                output = [
                    ResponseFunctionToolCall(
                        type="function_call",
                        call_id="call-1",
                        name="write_file",
                        arguments=json.dumps(
                            {"path": "approved.txt", "content": "approved"}
                        ),
                    )
                ]
            else:
                output = [
                    ResponseOutputMessage(
                        id="msg-1",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text", text="done", annotations=[]
                            )
                        ],
                    )
                ]
            return ModelResponse(output=output, usage=Usage(), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError
            yield  # pragma: no cover

    async with HubConnector(
        "http://test", transport=httpx.ASGITransport(app=hub.app)
    ) as connector:
        protocol = await connector.fetch_protocol()
        me = await connector.register("openai-worker", protocol.version)
        approvals = openai_agent.OperatorApprovals(connector, me.token, me.project)
        workspace = openai_agent.Workspace(tmp_path, approvals.request, "auto")
        agent = Agent(
            name="worker",
            model=ScriptedModel(),
            tools=openai_agent.build_tools(connector, me.token, "worker", workspace),
        )
        run = asyncio.create_task(
            Runner.run(
                agent,
                "Write the approved file",
                run_config=RunConfig(tracing_disabled=True),
            )
        )
        try:

            async def wait_for_form():
                while not approvals.pending:
                    if run.done():
                        run.result()
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_form(), timeout=3)
            assert not (tmp_path / "approved.txt").exists()
            form_id = next(iter(approvals.pending))
            assert hub.state is state
            assert state.answer_form(form_id, {"decision": "Approve"}) is not None
            inbound = await connector.receive(me.token, 0.1)
            assert any(m.get("kind") == "answer" for m in inbound.messages), inbound
            approvals.handle_inbound(inbound.messages)
            assert approvals.pending[form_id].done(), inbound
            result = await asyncio.wait_for(run, timeout=3)
            assert result.final_output == "done"
            assert (tmp_path / "approved.txt").read_text() == "approved"
            assert agent.model.calls == 2
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            await connector.leave(me.token)


async def test_stop_aborts_a_worker_waiting_for_approval(monkeypatch):
    from caucus.hub_connector import Inbound
    from caucus.native_agent import _run_loop

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    class Connector:
        async def receive(self, *args, **kwargs):
            await started.wait()
            return Inbound(messages=[], mode="stopped", stop=True, commands=[])

        async def set_status(self, *args):
            pass

    monkeypatch.setattr(openai_agent.Runner, "run", run)
    await asyncio.wait_for(
        _run_loop(
            lambda: openai_agent.OpenAIClient(SimpleNamespace(), max_turns=3),
            Connector(),
            "tok",
            poll_timeout=0,
            mission="start",
            drain_on_stop=False,
        ),
        timeout=1,
    )
    assert cancelled.is_set()


async def test_model_failure_ends_the_session(monkeypatch):
    from caucus.native_agent import _run_loop

    async def fail(*args, **kwargs):
        raise RuntimeError("model failed")

    class Connector:
        async def receive(self, *args, **kwargs):
            await asyncio.Future()

        async def set_status(self, *args):
            pass

    monkeypatch.setattr(openai_agent.Runner, "run", fail)
    with pytest.raises(RuntimeError, match="model failed"):
        await asyncio.wait_for(
            _run_loop(
                lambda: openai_agent.OpenAIClient(SimpleNamespace(), max_turns=3),
                Connector(),
                "tok",
                poll_timeout=0,
                mission="start",
                drain_on_stop=False,
            ),
            timeout=1,
        )


async def test_client_keeps_history_and_interrupts_only_current_turn(monkeypatch):
    calls = []
    blocked = asyncio.Event()

    async def run(agent, items, **kwargs):
        calls.append(list(items))
        if items[-1]["content"] == "blocked":
            blocked.set()
            await asyncio.Future()
        return SimpleNamespace(
            final_output="done",
            to_input_list=lambda: [*items, {"role": "assistant", "content": "done"}],
        )

    monkeypatch.setattr(openai_agent.Runner, "run", run)
    client = openai_agent.OpenAIClient(SimpleNamespace(), max_turns=10)
    async with client:
        await client.query("first")
        assert [m async for m in client.receive_response()]
        await client.query("blocked")
        response = asyncio.create_task(anext(client.receive_response()))
        await blocked.wait()
        await client.interrupt()
        with pytest.raises(StopAsyncIteration):
            await response
        await client.query("third")
        assert [m async for m in client.receive_response()]
    assert calls[2] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "third"},
    ]


class ApprovalConnector:
    """Return a known form ID and record approval requests."""

    async def join_channel(self, token, channel):
        return openai_agent.ChannelOutcome.OK

    async def ask_operator(self, token, to, title, fields):
        return SimpleNamespace(form_id="form-1")

    async def send(self, token, to, content):
        return SimpleNamespace(message_id="msg-1")

    async def leave_channel(self, token, channel):
        return openai_agent.ChannelOutcome.OK


async def test_shell_environment_and_process_group_cancellation(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("OPENAI_API_KEY", "should-not-reach-shell")

    async def approve(action, detail):
        return True

    workspace = openai_agent.Workspace(tmp_path, approve, "auto")
    assert "absent" in await workspace.run_shell(
        'test -z "$OPENAI_API_KEY" && echo absent'
    )
    task = asyncio.create_task(
        workspace.run_shell("echo $$ > shell.pid; sleep 30 & wait")
    )
    while not (tmp_path / "shell.pid").exists():
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    pid = int((tmp_path / "shell.pid").read_text())
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
    for _ in range(100):
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("shell descendants survived cancellation")


async def test_fast_operator_answer_is_buffered_until_form_id_is_known():
    class FastConnector(ApprovalConnector):
        async def ask_operator(self, token, to, title, fields):
            early = {
                "kind": "answer",
                "origin": "operator",
                "meta": {
                    "form_id": "form-1",
                    "to": to,
                    "status": "answered",
                    "answers": {"decision": "Approve"},
                },
            }
            assert approvals.handle_inbound([early]) == []
            return SimpleNamespace(form_id="form-1")

    approvals = openai_agent.OperatorApprovals(FastConnector(), "tok", "worker")
    assert await asyncio.wait_for(
        approvals.request("write", "exact content"), timeout=1
    )
    await approvals.close()


@pytest.mark.parametrize("runtime,mode", [("openai", "plan"), ("openai", "default")])
def test_supervisor_accepts_openai_form_approval_modes(tmp_path, runtime, mode):
    from caucus.supervisor import AgentSpec, AgentSupervisor, LauncherConfig

    sup = AgentSupervisor(
        LauncherConfig(enabled=True, cwd=tmp_path), "http://127.0.0.1:8765"
    )
    sup._validate(AgentSpec(name="bot", runtime=runtime, permission_mode=mode))


@pytest.mark.parametrize(
    "runtime,mode", [("openai", "bypassPermissions"), ("other", "auto")]
)
def test_supervisor_refuses_invalid_runtime_or_openai_mode(tmp_path, runtime, mode):
    from caucus.supervisor import (
        AgentSpec,
        AgentSupervisor,
        LauncherConfig,
        LauncherRefused,
    )

    sup = AgentSupervisor(
        LauncherConfig(enabled=True, cwd=tmp_path), "http://127.0.0.1:8765"
    )
    with pytest.raises(LauncherRefused):
        sup._validate(AgentSpec(name="bot", runtime=runtime, permission_mode=mode))


async def test_approval_requires_operator_provenance_and_matching_form():
    approvals = openai_agent.OperatorApprovals(ApprovalConnector(), "tok", "worker")
    task = asyncio.create_task(approvals.request("write_file", "file content"))
    while not approvals.pending:
        await asyncio.sleep(0)
    answer = {
        "kind": "answer",
        "origin": "agent",
        "meta": {
            "form_id": "form-1",
            "status": "answered",
            "answers": {"decision": "Approve"},
        },
    }
    assert approvals.handle_inbound([answer]) == [answer]
    assert not task.done()
    answer["origin"] = "operator"
    assert approvals.handle_inbound([answer]) == []
    assert await task
    assert not approvals.pending


async def test_workspace_rejects_escape_and_waits_for_write_approval(tmp_path):
    requests = []

    async def reject(action, detail):
        requests.append((action, detail))
        return False

    workspace = openai_agent.Workspace(tmp_path, reject, "auto")
    with pytest.raises(ValueError, match="workspace"):
        workspace.resolve("../outside")
    outside = tmp_path.parent / "outside-link-target"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="workspace"):
        workspace.resolve("escape/file")
    assert "denied" in await workspace.write_file("new.py", "secret")
    assert not (tmp_path / "new.py").exists()
    assert requests == [("write_file", "new.py\nsecret")]


async def test_worker_and_talker_have_distinct_tool_surfaces(tmp_path):
    tools = openai_agent.build_tools(ApprovalConnector(), "tok", "talker", None)
    assert {t.name for t in tools} == {
        "say",
        "protocol_section",
        "list_peers",
        "ask_operator",
        "list_forms",
        "join_channel",
        "leave_channel",
        "set_channel_topic",
        "floor",
    }
    workspace = openai_agent.Workspace(tmp_path, None, "acceptEdits")
    tools = openai_agent.build_tools(ApprovalConnector(), "tok", "worker", workspace)
    assert {"read_file", "write_file", "edit_file", "search_files", "run_shell"} <= {
        t.name for t in tools
    }
    write = next(t for t in tools if t.name == "write_file")
    from agents.tool_context import ToolContext

    arguments = json.dumps({"path": "x", "content": "hello"})
    context = ToolContext(
        context=None, tool_name="write_file", tool_call_id="1", tool_arguments=arguments
    )
    await write.on_invoke_tool(context, arguments)
    assert (tmp_path / "x").read_text() == "hello"


async def test_plan_has_no_write_or_shell_tools(tmp_path):
    workspace = openai_agent.Workspace(tmp_path, None, "plan")
    tools = openai_agent.build_tools(ApprovalConnector(), "tok", "worker", workspace)
    names = {t.name for t in tools}
    assert "read_file" in names
    assert not names & {"write_file", "edit_file", "run_shell"}


def test_openai_launcher_selects_runtime_without_leaking_key_to_claude(monkeypatch):
    from caucus.supervisor import AgentSpec, AgentSupervisor, LauncherConfig

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sup = AgentSupervisor(LauncherConfig(), hub_url="http://127.0.0.1:8765")
    spec = AgentSpec(name="bot", runtime="openai")
    assert sup._command(spec)[1:3] == ["-m", "caucus.openai_agent"]
    assert sup._build_env(spec)["OPENAI_API_KEY"] == "test-key"
    assert "OPENAI_API_KEY" not in sup._build_env(AgentSpec(name="claude"))
