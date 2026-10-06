"""SDK-independent workspace tools and server-attested operator approvals."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import secrets
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from .hub_connector import ChannelOutcome, HubConnector

Approval = Callable[[str, str], Awaitable[bool]]
MAX_OUTPUT_CHARS = 20_000


class OperatorApprovals:
    """Resolve action approvals only from server-attested operator answers.

    A private channel carries the full action and the resulting form. No peer
    text, sender name, unrelated form, or cancelled form can authorize a tool.
    Requests are serialized so a fast answer can be matched to its one form.
    """

    def __init__(self, connector: HubConnector, token: str, project: str) -> None:
        """Bind the approval gate to this membership and a private channel."""
        self.connector = connector
        self.token = token
        self.channel = f"#approval-{secrets.token_hex(8)}"
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self._lock = asyncio.Lock()
        self._joined = False
        self._opening = False
        self._early_answers: list[dict[str, object]] = []

    async def request(self, action: str, detail: str) -> bool:
        """Publish the exact action, then await approval for at most five minutes.

        Failure to deliver the action or open a form denies the operation. An
        edit approval covers only the exact content shown, never future calls.
        """
        async with self._lock:
            if await self.connector.join_channel(self.token, self.channel) is not (
                ChannelOutcome.OK
            ):
                return False
            self._joined = True
            delivery = await self.connector.send(
                self.token, self.channel, f"[workspace action]\n{action}\n{detail}"
            )
            if not delivery.message_id:
                return False
            self._opening = True
            try:
                return await self._open_form(action)
            finally:
                self._opening = False
                self._early_answers.clear()

    async def _open_form(self, action: str) -> bool:
        """Bind a form ID before releasing buffered operator responses."""
        form = await self.connector.ask_operator(
            self.token,
            self.channel,
            f"Approve {action}?",
            [
                {
                    "key": "decision",
                    "label": "Review the exact action in the "
                    "channel, then approve or reject it.",
                    "type": "radio",
                    "options": ["Reject", "Approve"],
                    "required": True,
                }
            ],
        )
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[form.form_id] = future
        self._opening = False
        self.handle_inbound(self._early_answers)
        self._early_answers.clear()
        try:
            return await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(form.form_id, None)

    async def close(self) -> None:
        """Release the approval channel on reset so subscriptions cannot pile up."""
        for future in self.pending.values():
            future.cancel()
        self.pending.clear()
        if self._joined:
            with contextlib.suppress(httpx.HTTPError):
                await self.connector.leave_channel(self.token, self.channel)
            self._joined = False

    def handle_inbound(
        self, messages: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Consume matching operator answers and leave room traffic queued."""
        remaining = []
        for message in messages:
            meta = message.get("meta")
            if (
                message.get("origin") != "operator"
                or message.get("kind") != "answer"
                or not isinstance(meta, dict)
            ):
                remaining.append(message)
                continue
            future = self.pending.get(str(meta.get("form_id")))
            if future is None:
                if self._opening and meta.get("to") == self.channel:
                    self._early_answers.append(message)
                    self._early_answers[:] = self._early_answers[-8:]
                    continue
                remaining.append(message)
                continue
            answers = meta.get("answers")
            approved = (
                meta.get("status") == "answered"
                and isinstance(answers, dict)
                and answers.get("decision") == "Approve"
            )
            if not future.done():
                future.set_result(approved)
        return remaining


class Workspace:
    """Bound file operations to a workspace and gate writes and shell execution.

    File tools reject symlink escapes. A shell is a host process with the user's
    privileges, not a sandbox: its exact command always needs operator approval.
    Shell children receive a small environment without API or Caucus credentials.
    """

    def __init__(
        self, root: Path, approval: Approval | None, permission_mode: str
    ) -> None:
        """Set the file root and the operator-approved permission policy."""
        self.root = root.resolve(strict=True)
        self.approval = approval
        self.permission_mode = permission_mode

    def resolve(self, path: str) -> Path:
        """Resolve a path and refuse traversal or symlinks outside the workspace."""
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("path escapes the workspace")
        return target

    async def _allowed(self, action: str, detail: str) -> bool:
        """Enforce the local policy independently of the model's instructions."""
        if self.permission_mode == "plan":
            return False
        if action != "run_shell" and self.permission_mode == "acceptEdits":
            return True
        return self.approval is not None and await self.approval(action, detail)

    async def read_file(self, path: str, start_line: int = 1, lines: int = 200) -> str:
        """Read a bounded, line-numbered UTF-8 slice of a workspace file."""
        if start_line < 1 or not 1 <= lines <= 1000:
            raise ValueError("start_line must be positive and lines must be 1-1000")
        target = self.resolve(path)
        if target.stat().st_size > 2_000_000:
            raise ValueError("file exceeds the 2 MB read limit")
        content = target.read_text().splitlines()
        return "\n".join(
            f"{i + 1}: {line}"
            for i, line in enumerate(content)
            if start_line <= i + 1 < start_line + lines
        )[:MAX_OUTPUT_CHARS]

    async def write_file(self, path: str, content: str) -> str:
        """Create or replace a UTF-8 file after approval for its exact content."""
        self.resolve(path)
        if not await self._allowed("write_file", f"{path}\n{content}"):
            return "denied: operator did not approve the write"
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path}"

    async def edit_file(self, path: str, old: str, new: str) -> str:
        """Replace one unique literal occurrence, checking for approval-time drift."""
        target = self.resolve(path)
        before = target.read_text()
        if not old or before.count(old) != 1:
            raise ValueError("old text must match exactly once")
        if not await self._allowed("edit_file", f"{path}\nOLD:\n{old}\nNEW:\n{new}"):
            return "denied: operator did not approve the edit"
        target = self.resolve(path)
        if target.read_text() != before:
            return "refused: file changed while awaiting approval; read it again"
        target.write_text(before.replace(old, new, 1))
        return f"edited {path}"

    async def search_files(self, pattern: str = "**/*", text: str = "") -> str:
        """Find workspace paths and optional literal text, returning bounded hits."""
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("search pattern escapes the workspace")
        hits = []
        for path in self.root.glob(pattern):
            if not path.is_file() or not path.resolve().is_relative_to(self.root):
                continue
            relative = path.relative_to(self.root)
            if any(
                part in {".git", ".venv", "node_modules"} for part in relative.parts
            ):
                continue
            if text:
                if path.stat().st_size > 2_000_000:
                    continue
                try:
                    content = path.read_text()
                except (UnicodeError, OSError):
                    continue
                for i, line in enumerate(content.splitlines(), 1):
                    if text in line:
                        hits.append(f"{relative}:{i}: {line[:500]}")
                    if len(hits) >= 100:
                        break
            else:
                hits.append(str(relative))
            if len(hits) >= 100:
                break
        return "\n".join(hits)[:MAX_OUTPUT_CHARS] or "no matches"

    async def run_shell(self, command: str, timeout: float = 60) -> str:
        """Run an approved Bash command and terminate its group on cancel/timeout."""
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be greater than 0 and at most 120 seconds")
        if not await self._allowed("run_shell", command):
            return "denied: operator did not approve the shell command"
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "TMPDIR",
                "SYSTEMROOT",
                "COMSPEC",
            }
        }
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            command,
            cwd=self.root,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

        async def collect() -> str:
            """Drain output without retaining more than the advertised bound."""
            chunks = bytearray()
            assert process.stdout is not None
            while chunk := await process.stdout.read(4096):
                chunks.extend(chunk[: max(0, MAX_OUTPUT_CHARS - len(chunks))])
            await process.wait()
            return f"exit={process.returncode}\n{chunks.decode(errors='replace')}"

        try:
            return await asyncio.wait_for(collect(), timeout)
        finally:
            # Kill descendants even when their shell exited ahead of them.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
