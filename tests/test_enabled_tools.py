"""Tests for the PROMETHEUS_MCP_ENABLED_TOOLS server-side tool allowlist."""

import asyncio
import importlib
import sys

import pytest


def _reload_server():
    """Reimport prometheus_mcp_server.server so module-level env reads re-run."""
    sys.modules.pop("prometheus_mcp_server.server", None)
    return importlib.import_module("prometheus_mcp_server.server")


@pytest.fixture
def clean_env(monkeypatch):
    """Provide a clean PROMETHEUS_* env for each test and restore the original
    prometheus_mcp_server.server module identity on teardown so other test
    files (which import the module once and patch attributes on it) are not
    affected by our reload."""
    original_module = sys.modules.get("prometheus_mcp_server.server")
    for key in (
        "TOOL_PREFIX",
        "PROMETHEUS_MCP_ENABLED_TOOLS",
    ):
        monkeypatch.delenv(key, raising=False)
    # PROMETHEUS_URL just needs to be set so config builds successfully.
    monkeypatch.setenv("PROMETHEUS_URL", "http://localhost:9090")
    yield monkeypatch
    if original_module is not None:
        sys.modules["prometheus_mcp_server.server"] = original_module
    else:
        sys.modules.pop("prometheus_mcp_server.server", None)


def _registered_tool_names(server_module):
    return sorted(tool.name for tool in asyncio.run(server_module.mcp.list_tools()))


def test_all_tools_registered_by_default(clean_env):
    """When PROMETHEUS_MCP_ENABLED_TOOLS is unset, every tool is registered."""
    server = _reload_server()

    assert server.ENABLED_TOOLS is None
    names = _registered_tool_names(server)
    # server_discover is registered by the spec2026 discovery layer
    assert "execute_query" in names
    assert "get_targets" in names
    assert "list_alerts" in names
    assert "server_discover" in names
    assert len(names) == 18


def test_enabled_tools_allowlist_filters_registry(clean_env):
    """Only tools listed in PROMETHEUS_MCP_ENABLED_TOOLS are registered."""
    clean_env.setenv(
        "PROMETHEUS_MCP_ENABLED_TOOLS",
        "execute_query,list_metrics",
    )
    server = _reload_server()

    assert server.ENABLED_TOOLS == {"execute_query", "list_metrics"}
    names = _registered_tool_names(server)
    # server_discover is always registered by the spec2026 layer
    assert "execute_query" in names
    assert "list_metrics" in names
    assert "server_discover" in names
    assert "get_targets" not in names


def test_enabled_tools_is_case_and_whitespace_insensitive(clean_env):
    """Allowlist matches base names case-insensitively and trims whitespace."""
    clean_env.setenv(
        "PROMETHEUS_MCP_ENABLED_TOOLS",
        " EXECUTE_QUERY , Execute_Range_Query ,,list_metrics ",
    )
    server = _reload_server()

    names = _registered_tool_names(server)
    assert "execute_query" in names
    assert "execute_range_query" in names
    assert "list_metrics" in names
    assert "get_targets" not in names


def test_enabled_tools_works_with_tool_prefix(clean_env):
    """Allowlist is checked against the unprefixed base name."""
    clean_env.setenv("TOOL_PREFIX", "staging")
    clean_env.setenv(
        "PROMETHEUS_MCP_ENABLED_TOOLS",
        "execute_query,list_metrics",
    )
    server = _reload_server()

    names = _registered_tool_names(server)
    assert "staging_execute_query" in names
    assert "staging_list_metrics" in names
    assert "staging_server_discover" in names
    assert "staging_get_targets" not in names


def test_empty_enabled_tools_falls_back_to_all_tools(clean_env):
    """An empty / whitespace-only env var is treated as 'no allowlist'."""
    clean_env.setenv("PROMETHEUS_MCP_ENABLED_TOOLS", "   ")
    server = _reload_server()

    assert server.ENABLED_TOOLS is None
    assert len(_registered_tool_names(server)) == 18
