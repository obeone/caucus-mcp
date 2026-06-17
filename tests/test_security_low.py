"""Regression tests for the Low-severity security hardening in caucus-mcp.

Covers eight behaviors introduced in the low-severity pass:

1. HUB body-size limit — BodySizeLimitMiddleware returns 413 on oversized bodies.
2. HUB /export token parity — /export gates on operator/observer token when auth
   is enabled, mirrors /ui requirements.
3. HUB console CSP — GET "/" response carries the CONSOLE_CSP header.
4. BRIDGE token file — _write_token_file uses mkstemp (mode 0600, random name).
5. BRIDGE resilient decorator — _resilient_hub_call converts HTTPError /
   JSONDecodeError to a structured hub_unreachable dict; success passes through.
6. DISKLOG atomic prune — a failure in os.replace leaves the original intact.
7. CLAUDE_AGENT backoff — _run_loop catches HTTPError from receive() and retries
   rather than dying.
8. WATCH malformed body — non-JSON 200 body is handled gracefully (OPTIONAL).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import caucus.hub as hub_module
from caucus.hub import MAX_BODY_BYTES, CONSOLE_CSP, AuthConfig


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _register(client: TestClient, project: str) -> str:
    """Register ``project`` via /register and return its token."""
    resp = client.post("/register", json={"project": project})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


# ===========================================================================
# 1. HUB body-size limit (BodySizeLimitMiddleware)
# ===========================================================================


def test_body_size_limit_oversized_post_returns_413(client: TestClient) -> None:
    """A POST body larger than MAX_BODY_BYTES must be rejected with 413.

    TestClient sets a Content-Length header, so the middleware's cheap
    header-check path is exercised: the oversized body is refused before
    a single byte is read.
    """
    # Build a body clearly over the 64 KiB cap; the simplest shape is a JSON
    # object whose "project" value is padded far past the limit.
    oversized_project = "x" * (MAX_BODY_BYTES + 1)
    # Raw bytes of the JSON document — large enough to trip the header check.
    body = json.dumps({"project": oversized_project}).encode("utf-8")
    assert len(body) > MAX_BODY_BYTES

    resp = client.post(
        "/register",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    data = resp.json()
    assert data["detail"] == "request body too large"
    assert data["max_bytes"] == MAX_BODY_BYTES


def test_body_size_limit_normal_post_succeeds(client: TestClient) -> None:
    """A small, well-formed POST must still reach the handler and return 200."""
    resp = client.post("/register", json={"project": "sanity-check"})
    assert resp.status_code == 200
    assert "token" in resp.json()


def test_body_size_limit_constant_matches_middleware(client: TestClient) -> None:
    """MAX_BODY_BYTES must equal exactly 64 * 1024 (the documented cap)."""
    assert MAX_BODY_BYTES == 64 * 1024


# ===========================================================================
# 2. HUB /export token parity
# ===========================================================================


@pytest.fixture
def with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure operator/observer tokens on the hub for the test's duration.

    Mirrors the fixture in test_security.py so the two modules use the same
    token values (operator="op-tok", observer="ob-tok").
    """
    monkeypatch.setattr(
        hub_module, "auth_config", AuthConfig(operator="op-tok", observer="ob-tok")
    )


def test_export_no_token_returns_401_when_auth_enabled(
    client: TestClient, with_auth: None
) -> None:
    """GET /export without Authorization must return 401 when auth is enabled."""
    resp = client.get("/export")
    assert resp.status_code == 401


def test_export_wrong_token_returns_401_when_auth_enabled(
    client: TestClient, with_auth: None
) -> None:
    """GET /export with a wrong Bearer token must return 401."""
    resp = client.get("/export", headers={"Authorization": "Bearer wrong-tok"})
    assert resp.status_code == 401


def test_export_operator_token_returns_200_when_auth_enabled(
    client: TestClient, with_auth: None
) -> None:
    """GET /export with the operator token must return 200 when auth is enabled."""
    resp = client.get("/export", headers={"Authorization": "Bearer op-tok"})
    assert resp.status_code == 200


def test_export_observer_token_returns_200_when_auth_enabled(
    client: TestClient, with_auth: None
) -> None:
    """GET /export with the observer (read-only) token must return 200.

    Observers are allowed to read the transcript; only write operations
    require the operator role.
    """
    resp = client.get("/export", headers={"Authorization": "Bearer ob-tok"})
    assert resp.status_code == 200


def test_export_open_when_auth_disabled(client: TestClient) -> None:
    """GET /export with no token must return 200 when auth is disabled (default)."""
    resp = client.get("/export")
    assert resp.status_code == 200


# ===========================================================================
# 3. HUB console CSP
# ===========================================================================


def test_console_csp_present_on_index(client: TestClient) -> None:
    """GET '/' must carry a Content-Security-Policy header equal to CONSOLE_CSP.

    The built SPA index.html may not be present in a source-only checkout;
    when absent the endpoint returns 404.  Either way we verify the header
    is set (on 200) or that the 404 case is acceptable — but never that the
    header is missing on a 200.
    """
    resp = client.get("/")
    if resp.status_code == 404:
        # Acceptable: the UI bundle is not shipped with a source checkout.
        pytest.skip("built UI index.html not present; CSP header test skipped")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert csp == CONSOLE_CSP, f"unexpected CSP: {csp!r}"


def test_console_csp_contains_required_directives() -> None:
    """CONSOLE_CSP must contain the four key lockdown directives.

    The exact header value is asserted in the live-request test above; this
    unit check guards the constant itself so a accidental edit to hub.py is
    caught without needing a live server.
    """
    assert "default-src 'self'" in CONSOLE_CSP
    assert "script-src 'self'" in CONSOLE_CSP
    assert "object-src 'none'" in CONSOLE_CSP
    assert "frame-ancestors 'none'" in CONSOLE_CSP


# ===========================================================================
# 4. BRIDGE token file (_write_token_file)
# ===========================================================================


def test_write_token_file_creates_file_with_token_content() -> None:
    """_write_token_file must return a path that exists and holds the token."""
    from caucus.mcp_bridge import _write_token_file, _cleanup_token_file

    token = "test-token-abc123"
    path = _write_token_file(token)
    try:
        assert os.path.exists(path), f"token file not found: {path}"
        content = Path(path).read_text(encoding="utf-8")
        assert content == token
    finally:
        _cleanup_token_file()
        # Belt-and-suspenders: if _cleanup_token_file didn't remove it, do it.
        if os.path.exists(path):
            os.unlink(path)


def test_write_token_file_mode_is_0600() -> None:
    """_write_token_file must create the file with mode 0600 (owner-only)."""
    from caucus.mcp_bridge import _write_token_file, _cleanup_token_file

    path = _write_token_file("secret-token")
    try:
        mode_bits = os.stat(path).st_mode & 0o777
        assert mode_bits == 0o600, f"expected 0600, got {oct(mode_bits)}"
    finally:
        _cleanup_token_file()
        if os.path.exists(path):
            os.unlink(path)


def test_write_token_file_basename_is_unpredictable() -> None:
    """_write_token_file must NOT use the old predictable caucus-watch-<pid>.token form.

    mkstemp inserts a random component in the name; the old fixed PID-based
    path had a predictable-path race. We verify the returned filename is
    different from the legacy pattern (does not end with exactly the PID).
    """
    from caucus.mcp_bridge import _write_token_file, _cleanup_token_file

    path = _write_token_file("tok")
    try:
        basename = os.path.basename(path)
        # The new path must start with "caucus-watch-" (the mkstemp prefix) and
        # end with ".token" (the suffix), but must have a random component between
        # the prefix and suffix — not just the bare PID.
        assert basename.startswith("caucus-watch-"), f"unexpected prefix: {basename!r}"
        assert basename.endswith(".token"), f"unexpected suffix: {basename!r}"
        # A predictable PID-only name would be "caucus-watch-<int>.token";
        # mkstemp inserts extra random chars so the middle part is not purely numeric.
        # Extract the segment between prefix and suffix and confirm it is not just digits.
        middle = basename[len("caucus-watch-") : -len(".token")]
        assert middle, "empty middle segment"
        # mkstemp generates at least 8 random chars — the middle is NOT purely a PID.
        assert not middle.isdigit(), (
            f"token file basename looks like the old predictable PID form: {basename!r}"
        )
    finally:
        _cleanup_token_file()
        if os.path.exists(path):
            os.unlink(path)


# ===========================================================================
# 5. BRIDGE resilient decorator (_resilient_hub_call)
# ===========================================================================


def test_resilient_hub_call_passes_through_on_success() -> None:
    """_resilient_hub_call must return the wrapped function's value unchanged."""
    from caucus.mcp_bridge import _resilient_hub_call

    @_resilient_hub_call
    def _ok() -> dict[str, object]:
        return {"message_id": "abc", "delivered_to": ["beta"]}

    result = _ok()
    assert result == {"message_id": "abc", "delivered_to": ["beta"]}


def test_resilient_hub_call_converts_connect_error() -> None:
    """httpx.ConnectError (a subclass of HTTPError) must become hub_unreachable."""
    from caucus.mcp_bridge import _resilient_hub_call

    @_resilient_hub_call
    def _raises() -> dict[str, object]:
        raise httpx.ConnectError("connection refused")

    result = _raises()
    assert result["error"] == "hub_unreachable"
    assert "hub" in result
    assert isinstance(result["detail"], str)


def test_resilient_hub_call_converts_http_status_error() -> None:
    """httpx.HTTPStatusError must also become hub_unreachable."""
    from caucus.mcp_bridge import _resilient_hub_call

    @_resilient_hub_call
    def _raises() -> dict[str, object]:
        request = httpx.Request("GET", "http://127.0.0.1:8765/receive")
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("internal server error", request=request, response=response)

    result = _raises()
    assert result["error"] == "hub_unreachable"
    assert "detail" in result


def test_resilient_hub_call_converts_json_decode_error() -> None:
    """json.JSONDecodeError must also become hub_unreachable."""
    from caucus.mcp_bridge import _resilient_hub_call

    @_resilient_hub_call
    def _raises() -> dict[str, object]:
        raise json.JSONDecodeError("Expecting value", "<body>", 0)

    result = _raises()
    assert result["error"] == "hub_unreachable"
    assert "detail" in result
    assert "hub" in result


# ===========================================================================
# 6. DISKLOG atomic prune — os.replace failure leaves original intact
# ===========================================================================


def test_disklog_prune_atomic_replace_failure_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError in os.replace must not truncate or empty the original log.

    Prune writes to a sibling temp file and uses os.replace for the swap.
    If the replace fails, the original must be fully intact. This test
    monkeypatches os.replace (in the disklog module's namespace) to raise
    OSError, then asserts the original file is unchanged and no exception
    escapes (the non-fatal contract is preserved).
    """
    import time
    from caucus.disklog import DiskLog
    import caucus.disklog as disklog_module

    path = tmp_path / "log.jsonl"
    log = DiskLog(path, retention_hours=1.0)

    # Write two lines: one fresh (within retention), one expired (outside).
    now = time.time()
    old_ts = now - 4 * 3600  # 4 hours ago, outside the 1-hour window
    fresh_ts = now            # right now, within the window

    # Build minimal JSONL records with explicit timestamps.
    old_record = json.dumps(
        {
            "ts": _utc_iso(old_ts),
            "seq": 1,
            "sender": "alpha",
            "recipient": "all",
            "kind": "message",
            "content": "old line",
            "meta": {"id": "id1", "delivered_to": []},
        }
    )
    fresh_record = json.dumps(
        {
            "ts": _utc_iso(fresh_ts),
            "seq": 2,
            "sender": "beta",
            "recipient": "all",
            "kind": "message",
            "content": "fresh line",
            "meta": {"id": "id2", "delivered_to": []},
        }
    )
    original_content = old_record + "\n" + fresh_record + "\n"
    path.write_text(original_content, encoding="utf-8")

    # Monkeypatch os.replace inside the disklog module to simulate a failure
    # after the temp file has been written — the original must survive.
    monkeypatch.setattr(disklog_module.os, "replace", _always_raise_os_error)

    # prune() must not raise, even when os.replace fails.
    try:
        log.prune(now=now)
    except OSError:
        pytest.fail("prune() must not propagate OSError from os.replace")

    # The original file must be unchanged — not truncated, not emptied.
    assert path.exists(), "original log file was deleted"
    after = path.read_text(encoding="utf-8")
    assert after == original_content, (
        f"original file was modified after replace failure.\n"
        f"Expected:\n{original_content!r}\nGot:\n{after!r}"
    )


def _always_raise_os_error(*args: object, **kwargs: object) -> None:
    """Stand-in for os.replace that always raises OSError."""
    raise OSError("simulated replace failure")


def _utc_iso(ts: float) -> str:
    """Convert a Unix timestamp to a UTC ISO-8601 string (matches DiskLog format)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ===========================================================================
# 7. CLAUDE_AGENT backoff — _run_loop catches HTTPError and retries
# ===========================================================================


class _FakeClientNoOp:
    """Agent client stub that records queries and yields no response messages."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        # Empty async generator — no messages to yield.
        for _ in ():
            yield None  # type: ignore[misc]


class _ConnectorErrorThenStop:
    """Connector stub that raises HTTPError on first receive, then signals stop.

    Used to verify the loop survives a transient hub error and retries rather
    than propagating the exception.
    """

    def __init__(self) -> None:
        self._call_count = 0

    async def receive(self, token: str, timeout: float) -> Any:
        self._call_count += 1
        if self._call_count == 1:
            # Simulate a transient connection error on the first poll.
            raise httpx.ConnectError("hub unreachable")
        # On the second call, signal stop so the loop exits cleanly.
        from caucus.hub_connector import Inbound
        return Inbound(messages=[], mode="running", stop=True)


async def test_run_loop_survives_http_error_from_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_loop must catch httpx.HTTPError from connector.receive() and retry.

    A transient hub error (connection refused, 5xx, etc.) must not propagate
    out of the loop; instead the loop backs off and retries. After recovery
    (the next receive returns a stop signal) the loop exits cleanly.
    """
    from caucus.claude_agent import _run_loop

    # Monkeypatch asyncio.sleep to avoid real delays in the backoff.
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    client = _FakeClientNoOp()
    connector = _ConnectorErrorThenStop()

    # Must return without raising — the HTTPError is handled internally.
    await _run_loop(
        client,  # type: ignore[arg-type]
        connector,  # type: ignore[arg-type]
        "tok",
        poll_timeout=0.0,
        mission=None,
    )

    # The loop polled at least twice: once (error) and once (stop).
    assert connector._call_count >= 2, (
        f"expected at least 2 receive() calls, got {connector._call_count}"
    )
    # The backoff sleep was triggered at least once.
    assert len(slept) >= 1, "expected at least one backoff sleep"
    # The first backoff must be within _BACKOFF_MIN (plus some tolerance).
    from caucus.claude_agent import _BACKOFF_MIN
    assert slept[0] == pytest.approx(_BACKOFF_MIN)
    # The agent client was not queried (no messages were received before stop).
    assert client.queries == []


# ===========================================================================
# 8. WATCH malformed body (OPTIONAL)
# ===========================================================================


def test_watch_malformed_json_body_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response with a non-JSON body must not crash watch(); it backs off.

    This tests the ValueError/JSONDecodeError handler in the watch() loop.
    We stub _emit so we can see what was output, and drive the watch loop by
    monkeypatching the httpx response to return a non-JSON body on the first
    poll, then an empty-messages body on the second, then we make the loop exit
    by raising KeyboardInterrupt (or by injecting a stop).

    Because the watch() loop is synchronous and tight, we use a controlled
    sequence of responses via a patched httpx.Client context.
    """
    import time
    from caucus import watch as watch_module

    emitted: list[str] = []
    monkeypatch.setattr(watch_module, "_emit", emitted.append)

    # Patch time.sleep to avoid real delays in the backoff.
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)

    call_count = 0

    class _FakeResponse:
        """Controlled response sequence: bad JSON, then stop payload."""

        def __init__(self, call_index: int) -> None:
            self.status_code = 200
            self._index = call_index

        def json(self) -> Any:
            if self._index == 0:
                # First call: raise JSONDecodeError to simulate malformed body.
                raise json.JSONDecodeError("Expecting value", "<html>", 0)
            # Second call: return a stop signal so the loop exits.
            return {
                "messages": [
                    {
                        "sender": "human",
                        "recipient": "all",
                        "content": "stop",
                        "kind": "control",
                    }
                ]
            }

    class _FakeHttp:
        """Minimal synchronous HTTP client stub."""

        def get(self, url: str, *, params: Any = None, headers: Any = None) -> _FakeResponse:
            nonlocal call_count
            resp = _FakeResponse(call_count)
            call_count += 1
            return resp

        def post(self, url: str, *, json: Any = None) -> _FakeResponse:
            return _FakeResponse(999)  # ACK — always succeeds

        def __enter__(self) -> "_FakeHttp":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    # Patch httpx.Client so watch() uses our fake.
    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeHttp())

    # watch() must return without raising; the non-JSON body is treated as a
    # transient error and the loop retries, eventually hitting the stop.
    result = watch_module.watch("http://127.0.0.1:8765", "test-token", 1.0)

    # The loop must have backed off at least once (after the bad JSON).
    assert len(slept) >= 1, "expected at least one backoff sleep after malformed body"
    # And it must have exited cleanly (0 == stop or emitted chatter).
    assert result == 0
