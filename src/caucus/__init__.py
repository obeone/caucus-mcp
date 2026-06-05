"""Caucus: a supervised message hub for multiple agents (any MCP client)."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version declared in pyproject.toml, read from
    # the installed package metadata. Nothing else hardcodes it.
    __version__ = version("caucus-mcp")
except PackageNotFoundError:  # running from a source tree with no install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
