"""Regression test for the ``mcp`` dependency ceiling.

``mcp`` 2.0.0 removed ``mcp.server.fastmcp``, which both :mod:`caucus.mcp_bridge`
and :mod:`caucus.mcp_http` import at module scope. Publishing with an unbounded
``mcp>=1.9`` therefore shipped a package that a fresh install could not run at
all: the bridge died on import, and the hub died at boot, because
``_mount_mcp_http`` imports ``mcp_http`` lazily on a loopback bind.

This asserts the declared metadata, not the resolved environment, so it fails in
CI the moment someone drops the ceiling — the mistake it exists to catch — rather
than only on whichever machine happens to resolve a 2.x.
"""

from __future__ import annotations

import importlib.metadata as metadata

import pytest
from packaging.requirements import Requirement


def _mcp_requirement() -> Requirement:
    """Return the declared ``mcp`` requirement from the installed metadata."""
    for raw in metadata.requires("caucus-mcp") or []:
        req = Requirement(raw)
        # Skip the extras (e.g. the `claude` group): they carry a marker.
        if req.name == "mcp" and not req.marker:
            return req
    pytest.fail("caucus-mcp no longer declares an `mcp` dependency")


def test_mcp_dependency_excludes_the_2x_series() -> None:
    """The declared range must not admit an mcp release without fastmcp."""
    spec = _mcp_requirement().specifier
    assert not spec.contains("2.0.0"), (
        "mcp 2.x removed mcp.server.fastmcp, which mcp_bridge and mcp_http import;"
        " keep the upper bound until both are ported to the 2.x server API"
    )


def test_mcp_dependency_keeps_its_floor() -> None:
    """The 1.9 floor still holds: below it there is no transport_security."""
    spec = _mcp_requirement().specifier
    assert not spec.contains("1.8.0")
    assert spec.contains("1.28.1")
