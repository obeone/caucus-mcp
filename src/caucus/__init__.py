"""Caucus: a supervised message hub for multiple agents (any MCP client)."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Preferred source: the module hatch-vcs writes at build time from the git
    # tag (see `[tool.hatch.build.hooks.vcs]` in pyproject.toml). Present in
    # every wheel and after any `pip install`/`uv pip install` (incl. editable).
    from caucus._version import __version__
except ImportError:  # pragma: no cover - _version.py not generated yet
    try:
        # Fallback: read it from the installed package metadata. Covers the case
        # where the build hook did not run but the distribution is registered.
        __version__ = version("caucus-mcp")
    except PackageNotFoundError:  # running from a bare source tree, no install
        __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
