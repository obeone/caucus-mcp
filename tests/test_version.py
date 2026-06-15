"""Regression tests for the ``--version`` flag on all four Caucus executables.

Each ``main()`` is called with ``sys.argv`` patched to ``["prog", "--version"]``.
argparse's ``action="version"`` prints the version to stdout and raises
``SystemExit(0)``; these tests verify both behaviours, and that the printed
string contains ``caucus.__version__``.

Note: ``mcp_bridge.main()`` would normally call ``mcp.run()`` (blocking I/O)
when given any other arguments — only the ``--version`` path, which exits
before that call, is tested here.
"""

from __future__ import annotations

import sys

import pytest

import caucus
import caucus.claude_agent
import caucus.hub
import caucus.mcp_bridge
import caucus.watch


@pytest.mark.parametrize(
    ("main_func", "label"),
    [
        (caucus.hub.main, "hub"),
        (caucus.watch.main, "watch"),
        (caucus.claude_agent.main, "claude_agent"),
        (caucus.mcp_bridge.main, "mcp_bridge"),
    ],
    ids=["hub", "watch", "claude_agent", "mcp_bridge"],
)
def test_version_flag_prints_version_and_exits_zero(
    main_func: object,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--version`` on each executable prints the package version and exits 0.

    Parameters
    ----------
    main_func:
        The ``main()`` callable of the executable under test.
    label:
        Human-readable name used only for identification in failure output.
    monkeypatch:
        pytest fixture for patching ``sys.argv``.
    capsys:
        pytest fixture for capturing stdout/stderr output.
    """
    monkeypatch.setattr(sys, "argv", ["prog", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        main_func()  # type: ignore[operator]

    assert exc_info.value.code == 0, f"{label}: expected exit code 0"

    captured = capsys.readouterr()
    output = captured.out + captured.err  # argparse may write to either
    assert caucus.__version__ in output, (
        f"{label}: expected {caucus.__version__!r} in output, got {output!r}"
    )
    assert caucus.__version__, f"{label}: caucus.__version__ must be non-empty"
