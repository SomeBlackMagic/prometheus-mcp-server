"""Tests for the MCP tools functionality."""

import pytest
import json
import time
from unittest.mock import patch, MagicMock
from fastmcp import Client
from prometheus_mcp_server.server import (
    mcp, execute_query, execute_range_query, list_metrics, get_metric_metadata, get_targets,
    list_alerts, list_rules, list_label_names, list_label_values, find_series,
    get_runtime_info, get_build_info, get_tsdb_stats, query_exemplars, format_query,
    get_target_metadata,
    _metrics_cache, clear_metrics_cache,
    _coerce_metadata_entries, _normalize_metadata_map, _metadata_matches_pattern,
    _is_legacy_label_rune, _escape_label_name,
)

@pytest.fixture(autouse=True)
def reset_metrics_cache():
    """Reset metrics cache before each test to prevent state leaking."""
    clear_metrics_cache()
    yield
    clear_metrics_cache()

@pytest.fixture
def mock_make_request():
    """Mock the make_prometheus_request function."""
    with patch("prometheus_mcp_server.server.make_prometheus_request") as mock:
        yield mock

@pytest.mark.asyncio
async def test_execute_query(mock_make_request):
    """Test the execute_query tool."""
    # Setup
    mock_make_request.return_value = {
        "resultType": "vector",
        "result": [{"metric": {"__name__": "up"}, "value": [1617898448.214, "1"]}]
    }

    async with Client(mcp) as client:
        # Execute
        result = await client.call_tool("execute_query", {"query":"up"})

        # Verify
        mock_make_request.assert_called_once_with("query", params={"query": "up"})
        assert result.data["resultType"] == "vector"
        assert len(result.data["result"]) == 1
        # Verify resource links are included (MCP 2025 feature)
        assert "links" in result.data
        assert len(result.data["links"]) > 0
        assert result.data["links"][0]["rel"] == "prometheus-ui"

@pytest.mark.asyncio
async def test_execute_query_with_time(mock_make_request):
    """Test the execute_query tool with a specified time."""
    # Setup
    mock_make_request.return_value = {
        "resultType": "vector",
        "result": [{"metric": {"__name__": "up"}, "value": [1617898448.214, "1"]}]
    }

    async with Client(mcp) as client:
        # Execute
        result = await client.call_tool("execute_query", {"query":"up", "time":"2023-01-01T00:00:00Z"})
        
        # Verify
        mock_make_request.assert_called_once_with("query", params={"query": "up", "time": "2023-01-01T00:00:00Z"})
        assert result.data["resultType"] == "vector"

@pytest.mark.asyncio
async def test_execute_range_query(mock_make_request):
    """Test the execute_range_query tool."""
    # Setup
    mock_make_request.return_value = {
        "resultType": "matrix",
        "result": [{
            "metric": {"__name__": "up"},
            "values": [
                [1617898400, "1"],
                [1617898415, "1"]
            ]
        }]
    }

    async with Client(mcp) as client:
        # Execute
        result = await client.call_tool(
            "execute_range_query",{
            "query": "up",
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z",
            "step": "15s"
        })

        # Verify
        mock_make_request.assert_called_once_with("query_range", params={
            "query": "up",
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z",
            "step": "15s"
        })
        assert result.data["resultType"] == "matrix"
        assert len(result.data["result"]) == 1
        assert len(result.data["result"][0]["values"]) == 2
        # Verify resource links are included (MCP 2025 feature)
        assert "links" in result.data
        assert len(result.data["links"]) > 0
        assert result.data["links"][0]["rel"] == "prometheus-ui"

@pytest.fixture
def mock_get_cached_metrics():
    """Mock the get_cached_metrics function."""
    with patch("prometheus_mcp_server.server.get_cached_metrics") as mock:
        yield mock

@pytest.mark.asyncio
async def test_list_metrics(mock_get_cached_metrics):
    """Test the list_metrics tool."""
    # Setup
    mock_get_cached_metrics.return_value = ["up", "go_goroutines", "http_requests_total"]

    async with Client(mcp) as client:
        # Execute - call without pagination
        result = await client.call_tool("list_metrics", {})

        # Verify
        mock_get_cached_metrics.assert_called_once()
        # Now returns a dict with pagination info
        assert result.data["metrics"] == ["up", "go_goroutines", "http_requests_total"]
        assert result.data["total_count"] == 3
        assert result.data["returned_count"] == 3
        assert result.data["offset"] == 0
        assert result.data["has_more"] == False

@pytest.mark.asyncio
async def test_list_metrics_with_pagination(mock_get_cached_metrics):
    """Test the list_metrics tool with pagination."""
    # Setup
    mock_get_cached_metrics.return_value = ["metric1", "metric2", "metric3", "metric4", "metric5"]

    async with Client(mcp) as client:
        # Execute - call with limit and offset
        result = await client.call_tool("list_metrics", {"limit": 2, "offset": 1})

        # Verify
        mock_get_cached_metrics.assert_called_once()
        assert result.data["metrics"] == ["metric2", "metric3"]
        assert result.data["total_count"] == 5
        assert result.data["returned_count"] == 2
        assert result.data["offset"] == 1
        assert result.data["has_more"] == True

@pytest.mark.asyncio
async def test_list_metrics_with_filter(mock_get_cached_metrics):
    """Test the list_metrics tool with filter pattern."""
    # Setup
    mock_get_cached_metrics.return_value = ["http_requests_total", "http_response_size", "go_goroutines", "up"]

    async with Client(mcp) as client:
        # Execute - call with filter
        result = await client.call_tool("list_metrics", {"filter_pattern": "http"})

        # Verify
        mock_get_cached_metrics.assert_called_once()
        assert result.data["metrics"] == ["http_requests_total", "http_response_size"]
        assert result.data["total_count"] == 2
        assert result.data["returned_count"] == 2
        assert result.data["offset"] == 0
        assert result.data["has_more"] == False

@pytest.mark.asyncio
async def test_list_metrics_refresh_cache(mock_get_cached_metrics):
    """Test that refresh_cache=True invalidates the cache before fetching."""
    # Pre-populate cache to simulate a warm cache
    _metrics_cache["data"] = ["old_metric"]
    _metrics_cache["timestamp"] = time.time()

    cache_state_at_call = {}
    def capture_cache_state():
        cache_state_at_call["data"] = _metrics_cache["data"]
        cache_state_at_call["timestamp"] = _metrics_cache["timestamp"]
        return ["new_metric"]

    mock_get_cached_metrics.side_effect = lambda: capture_cache_state()

    async with Client(mcp) as client:
        result = await client.call_tool("list_metrics", {"refresh_cache": True})

        mock_get_cached_metrics.assert_called_once()
        # Verify the cache was cleared before get_cached_metrics was called
        assert cache_state_at_call["data"] is None
        assert cache_state_at_call["timestamp"] == 0
        assert result.data["metrics"] == ["new_metric"]

@pytest.mark.asyncio
async def test_list_metrics_no_refresh_by_default(mock_get_cached_metrics):
    """Test that cache is not invalidated by default."""
    _metrics_cache["data"] = ["cached_metric"]
    original_timestamp = time.time()
    _metrics_cache["timestamp"] = original_timestamp

    mock_get_cached_metrics.return_value = ["cached_metric"]

    async with Client(mcp) as client:
        result = await client.call_tool("list_metrics", {})

        # Cache should not have been reset
        assert _metrics_cache["timestamp"] == original_timestamp
        assert _metrics_cache["data"] == ["cached_metric"]
        assert result.data["metrics"] == ["cached_metric"]

@pytest.mark.asyncio
async def test_get_metric_metadata(mock_make_request):
    """Test the get_metric_metadata tool."""
    # Setup
    mock_make_request.return_value = {"data": [
        {"metric": "up", "type": "gauge", "help": "Up indicates if the scrape was successful", "unit": ""}
    ]}

    async with Client(mcp) as client:
        # Execute
        result = await client.call_tool("get_metric_metadata", {"metric":"up"})

        payload = result.content[0].text
        json_data = json.loads(payload)

        # Verify
        mock_make_request.assert_called_once_with("metadata", params={"metric": "up"})
        assert len(json_data) == 1
        assert json_data[0]["metric"] == "up"
        assert json_data[0]["type"] == "gauge"

@pytest.mark.asyncio
async def test_get_metric_metadata_bulk(mock_make_request):
    """Test get_metric_metadata bulk mode without metric filter."""
    mock_make_request.return_value = {
        "up": [{"type": "gauge", "help": "Target availability", "unit": ""}],
        "tls_expiry_seconds": [{"type": "gauge", "help": "Seconds until certificate expiry", "unit": "seconds"}],
        "process_cpu_seconds_total": [{"type": "counter", "help": "Total CPU seconds", "unit": "seconds"}],
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_metric_metadata", {})

        payload = result.content[0].text
        json_data = json.loads(payload)

        mock_make_request.assert_called_once_with("metadata", params=None)
        assert json_data["total_count"] == 3
        assert json_data["returned_count"] == 3
        assert json_data["offset"] == 0
        assert json_data["has_more"] is False
        assert "tls_expiry_seconds" in json_data["metadata"]


@pytest.mark.asyncio
async def test_get_metric_metadata_filter_matches_description(mock_make_request):
    """Test get_metric_metadata filter_pattern on metadata descriptions."""
    mock_make_request.return_value = {
        "tls_expiry_seconds": [{"type": "gauge", "help": "Seconds until certificate expiry", "unit": "seconds"}],
        "process_cpu_seconds_total": [{"type": "counter", "help": "Total CPU seconds", "unit": "seconds"}],
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_metric_metadata", {"filter_pattern": "certificate"})

        payload = result.content[0].text
        json_data = json.loads(payload)

        mock_make_request.assert_called_once_with("metadata", params=None)
        assert json_data["total_count"] == 1
        assert "tls_expiry_seconds" in json_data["metadata"]
        assert "process_cpu_seconds_total" not in json_data["metadata"]


@pytest.mark.asyncio
async def test_get_metric_metadata_bulk_pagination(mock_make_request):
    """Test get_metric_metadata bulk pagination."""
    mock_make_request.return_value = {
        "metric_a": [{"type": "gauge", "help": "A", "unit": ""}],
        "metric_b": [{"type": "gauge", "help": "B", "unit": ""}],
        "metric_c": [{"type": "gauge", "help": "C", "unit": ""}],
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_metric_metadata", {"limit": 1, "offset": 1})

        payload = result.content[0].text
        json_data = json.loads(payload)

        mock_make_request.assert_called_once_with("metadata", params=None)
        assert json_data["total_count"] == 3
        assert json_data["returned_count"] == 1
        assert json_data["offset"] == 1
        assert json_data["has_more"] is True
        assert list(json_data["metadata"].keys()) == ["metric_b"]


@pytest.mark.asyncio
async def test_get_targets(mock_make_request):
    """Test the get_targets tool."""
    # Setup
    mock_make_request.return_value = {
        "activeTargets": [
            {"discoveredLabels": {"__address__": "localhost:9090"}, "labels": {"job": "prometheus"}, "health": "up"}
        ],
        "droppedTargets": []
    }

    async with Client(mcp) as client:
        # Execute
        result = await client.call_tool("get_targets",{})

        payload = result.content[0].text
        json_data = json.loads(payload)

        # Verify
        # Defaults to active-only, filtered server-side by Prometheus.
        mock_make_request.assert_called_once_with("targets", params={"state": "active"})
        assert len(json_data["activeTargets"]) == 1
        assert json_data["activeTargets"][0]["health"] == "up"
        assert len(json_data["droppedTargets"]) == 0
        assert json_data["total_active"] == 1
        assert json_data["total_dropped"] == 0
        assert json_data["state"] == "active"
        assert json_data["has_more"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,expected_params",
    [
        ("active", {"state": "active"}),
        ("dropped", {"state": "dropped"}),
        ("any", None),
    ],
)
async def test_get_targets_state_filter(mock_make_request, state, expected_params):
    """state is passed through to Prometheus; 'any' sends no filter."""
    mock_make_request.return_value = {"activeTargets": [], "droppedTargets": []}

    async with Client(mcp) as client:
        await client.call_tool("get_targets", {"state": state})

    mock_make_request.assert_called_once_with("targets", params=expected_params)


@pytest.mark.asyncio
async def test_get_targets_scrape_pool_filter(mock_make_request):
    """scrape_pool is passed through to Prometheus alongside state."""
    mock_make_request.return_value = {"activeTargets": [], "droppedTargets": []}

    async with Client(mcp) as client:
        await client.call_tool("get_targets", {"scrape_pool": "node_exporter"})

    mock_make_request.assert_called_once_with(
        "targets", params={"state": "active", "scrapePool": "node_exporter"}
    )


@pytest.mark.asyncio
async def test_get_targets_pagination(mock_make_request):
    """limit/offset slice each category and report accurate totals."""
    mock_make_request.return_value = {
        "activeTargets": [{"labels": {"i": str(i)}} for i in range(5)],
        "droppedTargets": [],
    }

    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_targets", {"state": "any", "limit": 2, "offset": 1}
        )
        json_data = json.loads(result.content[0].text)

    assert [t["labels"]["i"] for t in json_data["activeTargets"]] == ["1", "2"]
    assert json_data["total_active"] == 5
    assert json_data["returned_active"] == 2
    assert json_data["offset"] == 1
    assert json_data["has_more"] is True


@pytest.mark.asyncio
async def test_get_targets_offset_without_limit(mock_make_request):
    """offset alone skips without truncating the remainder."""
    mock_make_request.return_value = {
        "activeTargets": [{"labels": {"i": str(i)}} for i in range(3)],
        "droppedTargets": [],
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_targets", {"offset": 2})
        json_data = json.loads(result.content[0].text)

    assert json_data["returned_active"] == 1
    assert json_data["total_active"] == 3
    assert json_data["has_more"] is False


@pytest.mark.asyncio
async def test_get_targets_handles_missing_keys(mock_make_request):
    """A response omitting either category should not raise."""
    mock_make_request.return_value = {}

    async with Client(mcp) as client:
        result = await client.call_tool("get_targets", {})
        json_data = json.loads(result.content[0].text)

    assert json_data["activeTargets"] == []
    assert json_data["total_dropped"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": "bogus"},
        {"offset": -1},
        {"limit": -5},
    ],
)
async def test_get_targets_rejects_invalid_arguments(mock_make_request, kwargs):
    """Invalid arguments fail before any request is made."""
    from fastmcp.exceptions import ToolError

    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_targets", kwargs)

    mock_make_request.assert_not_called()


# --- Helper function unit tests ---

class TestCoerceMetadataEntries:
    """Tests for _coerce_metadata_entries edge cases."""

    def test_with_dict_value(self):
        """A single dict should be wrapped in a list."""
        result = _coerce_metadata_entries({"type": "gauge", "help": "Up"})
        assert result == [{"type": "gauge", "help": "Up"}]

    def test_with_unsupported_type(self):
        """Non-dict/non-list values should return empty list."""
        assert _coerce_metadata_entries("string") == []
        assert _coerce_metadata_entries(42) == []
        assert _coerce_metadata_entries(None) == []


class TestNormalizeMetadataMap:
    """Tests for _normalize_metadata_map edge cases."""

    def test_skips_non_string_keys(self):
        """Non-string dict keys should be ignored."""
        data = {"up": [{"type": "gauge"}]}
        data[123] = [{"type": "counter"}]
        result = _normalize_metadata_map(data)
        assert list(result.keys()) == ["up"]

    def test_dict_no_normalizable_entries_no_metric_key(self):
        """Dict with no coercible entries and no 'metric' key returns empty."""
        result = _normalize_metadata_map({"foo": "bar", "baz": 42})
        assert result == {}

    def test_list_skips_non_dict_entries(self):
        """Non-dict items in a list should be skipped."""
        result = _normalize_metadata_map([
            "not_a_dict",
            {"metric": "up", "type": "gauge"},
        ])
        assert list(result.keys()) == ["up"]

    def test_list_skips_entries_without_metric_key(self):
        """Dict entries without a 'metric' string key should be skipped."""
        result = _normalize_metadata_map([
            {"type": "gauge"},
            {"metric": "up", "type": "gauge"},
        ])
        assert list(result.keys()) == ["up"]

    def test_unsupported_type_returns_empty(self):
        """Non-dict/non-list input should return empty dict."""
        assert _normalize_metadata_map("string") == {}
        assert _normalize_metadata_map(42) == {}
        assert _normalize_metadata_map(None) == {}


class TestMetadataMatchesPattern:
    """Tests for _metadata_matches_pattern edge cases."""

    def test_matches_metric_name(self):
        """Pattern matching on metric name should return True."""
        assert _metadata_matches_pattern(
            "http_requests_total", [{"type": "counter"}], "http"
        ) is True

    def test_no_match(self):
        """Non-matching pattern should return False."""
        assert _metadata_matches_pattern(
            "up", [{"type": "gauge", "help": "availability"}], "http"
        ) is False


# --- MCP tool integration tests for edge cases ---

@pytest.mark.asyncio
async def test_get_metric_metadata_dict_entries(mock_make_request):
    """Test bulk mode when metadata values are dicts instead of lists."""
    mock_make_request.return_value = {
        "up": {"type": "gauge", "help": "Target availability", "unit": ""},
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_metric_metadata", {})
        payload = result.content[0].text
        json_data = json.loads(payload)

        assert json_data["total_count"] == 1
        assert "up" in json_data["metadata"]
        assert json_data["metadata"]["up"] == [{"type": "gauge", "help": "Target availability", "unit": ""}]


@pytest.mark.asyncio
async def test_get_metric_metadata_filter_matches_name(mock_make_request):
    """Test filter_pattern matching on metric name (not description)."""
    mock_make_request.return_value = {
        "http_requests_total": [{"type": "counter", "help": "Total requests", "unit": ""}],
        "go_goroutines": [{"type": "gauge", "help": "Number of goroutines", "unit": ""}],
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_metric_metadata", {"filter_pattern": "http"})
        payload = result.content[0].text
        json_data = json.loads(payload)

        assert json_data["total_count"] == 1
        assert "http_requests_total" in json_data["metadata"]
        assert "go_goroutines" not in json_data["metadata"]


@pytest.mark.asyncio
async def test_get_metric_metadata_fallback_entries(mock_make_request):
    """Test fallback when metric is not found in normalized map."""
    mock_make_request.return_value = [
        {"type": "gauge", "help": "Up status", "unit": ""}
    ]

    async with Client(mcp) as client:
        result = await client.call_tool("get_metric_metadata", {"metric": "up"})
        payload = result.content[0].text
        json_data = json.loads(payload)

        assert len(json_data) == 1
        assert json_data[0]["type"] == "gauge"


# --- Alerts & rules tools ---

@pytest.mark.asyncio
async def test_list_alerts(mock_make_request):
    """Test the list_alerts tool."""
    mock_make_request.return_value = {
        "alerts": [
            {
                "labels": {"alertname": "HighCPU", "severity": "critical"},
                "annotations": {"summary": "CPU usage is high"},
                "state": "firing",
                "activeAt": "2023-01-01T00:00:00Z",
                "value": "9.5e+01"
            }
        ]
    }

    async with Client(mcp) as client:
        result = await client.call_tool("list_alerts", {})

        mock_make_request.assert_called_once_with("alerts", params=None)
        assert result.data["alert_count"] == 1
        assert result.data["alerts"][0]["state"] == "firing"
        assert result.data["alerts"][0]["labels"]["alertname"] == "HighCPU"

@pytest.mark.asyncio
async def test_list_rules(mock_make_request):
    """Test the list_rules tool without filters."""
    mock_make_request.return_value = {
        "groups": [
            {
                "name": "example",
                "file": "/rules.yml",
                "rules": [
                    {"name": "HighCPU", "type": "alerting", "state": "firing", "query": "cpu > 90"}
                ]
            }
        ]
    }

    async with Client(mcp) as client:
        result = await client.call_tool("list_rules", {})

        mock_make_request.assert_called_once_with("rules", params=None)
        assert result.data["group_count"] == 1
        assert result.data["groups"][0]["name"] == "example"

@pytest.mark.asyncio
async def test_list_rules_with_type_filter(mock_make_request):
    """Test the list_rules tool with a rule type filter."""
    mock_make_request.return_value = {"groups": []}

    async with Client(mcp) as client:
        result = await client.call_tool("list_rules", {"type": "alert"})

        mock_make_request.assert_called_once_with("rules", params={"type": "alert"})
        assert result.data["group_count"] == 0

@pytest.mark.asyncio
async def test_list_rules_with_name_and_group_filters(mock_make_request):
    """Test the list_rules tool with rule_name and rule_group filters."""
    mock_make_request.return_value = {"groups": []}

    async with Client(mcp) as client:
        await client.call_tool("list_rules", {
            "rule_name": ["HighCPU"],
            "rule_group": ["example"]
        })

        mock_make_request.assert_called_once_with("rules", params={
            "rule_name[]": ["HighCPU"],
            "rule_group[]": ["example"]
        })

@pytest.mark.asyncio
async def test_list_rules_filters_client_side_when_server_ignores_params(mock_make_request):
    """Test list_rules post-filters results for servers that ignore rule_name[]/rule_group[]."""
    mock_make_request.return_value = {
        "groups": [
            {
                "name": "g1",
                "file": "/rules.yml",
                "rules": [
                    {"name": "HighCPU", "type": "alerting"},
                    {"name": "Other", "type": "alerting"}
                ]
            },
            {
                "name": "g2",
                "file": "/rules.yml",
                "rules": [{"name": "Other2", "type": "alerting"}]
            }
        ]
    }

    async with Client(mcp) as client:
        result = await client.call_tool("list_rules", {"rule_name": ["HighCPU"]})

        assert result.data["group_count"] == 1
        assert result.data["groups"][0]["name"] == "g1"
        assert [r["name"] for r in result.data["groups"][0]["rules"]] == ["HighCPU"]

@pytest.mark.asyncio
async def test_list_rules_filters_groups_client_side(mock_make_request):
    """Test list_rules post-filters by group name when the server ignores rule_group[]."""
    mock_make_request.return_value = {
        "groups": [
            {"name": "g1", "file": "/rules.yml", "rules": [{"name": "A", "type": "alerting"}]},
            {"name": "g2", "file": "/rules.yml", "rules": [{"name": "B", "type": "alerting"}]}
        ]
    }

    async with Client(mcp) as client:
        result = await client.call_tool("list_rules", {"rule_group": ["g2"]})

        assert result.data["group_count"] == 1
        assert result.data["groups"][0]["name"] == "g2"

@pytest.mark.asyncio
async def test_list_rules_rejects_invalid_type(mock_make_request):
    """Test the list_rules tool rejects an invalid rule type."""
    async with Client(mcp) as client:
        with pytest.raises(Exception, match="Invalid rule type"):
            await client.call_tool("list_rules", {"type": "bogus"})

        mock_make_request.assert_not_called()


# --- Label & series exploration tools ---

@pytest.mark.asyncio
async def test_list_label_names(mock_make_request):
    """Test the list_label_names tool."""
    mock_make_request.return_value = ["__name__", "instance", "job"]

    async with Client(mcp) as client:
        result = await client.call_tool("list_label_names", {})

        mock_make_request.assert_called_once_with("labels", params=None)
        assert result.data["labels"] == ["__name__", "instance", "job"]
        assert result.data["count"] == 3

@pytest.mark.asyncio
async def test_list_label_names_with_match_and_time_range(mock_make_request):
    """Test the list_label_names tool forwards match selectors and time range."""
    mock_make_request.return_value = ["job"]

    async with Client(mcp) as client:
        await client.call_tool("list_label_names", {
            "match": ["up", "process_start_time_seconds{job=\"prometheus\"}"],
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z"
        })

        mock_make_request.assert_called_once_with("labels", params={
            "match[]": ["up", "process_start_time_seconds{job=\"prometheus\"}"],
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z"
        })

@pytest.mark.asyncio
async def test_list_label_values(mock_make_request):
    """Test the list_label_values tool."""
    mock_make_request.return_value = ["prometheus", "node-exporter"]

    async with Client(mcp) as client:
        result = await client.call_tool("list_label_values", {"label_name": "job"})

        mock_make_request.assert_called_once_with("label/job/values", params=None)
        assert result.data["values"] == ["prometheus", "node-exporter"]
        assert result.data["count"] == 2

@pytest.mark.asyncio
async def test_list_label_values_with_match_and_time_range(mock_make_request):
    """Test the list_label_values tool forwards match selectors and time range."""
    mock_make_request.return_value = ["prometheus"]

    async with Client(mcp) as client:
        await client.call_tool("list_label_values", {
            "label_name": "job",
            "match": ["up"],
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z"
        })

        mock_make_request.assert_called_once_with("label/job/values", params={
            "match[]": ["up"],
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z"
        })

@pytest.mark.asyncio
async def test_list_label_values_rejects_empty_label_name(mock_make_request):
    """Test the list_label_values tool rejects an empty label name."""
    async with Client(mcp) as client:
        with pytest.raises(Exception, match="label_name must not be empty"):
            await client.call_tool("list_label_values", {"label_name": ""})

        mock_make_request.assert_not_called()

@pytest.mark.asyncio
async def test_list_label_values_escapes_utf8_label_name(mock_make_request):
    """Test that UTF-8 label names get Prometheus U__ values-escaping in the URL path."""
    mock_make_request.return_value = ["frontend"]

    async with Client(mcp) as client:
        await client.call_tool("list_label_values", {"label_name": "app.kubernetes.io/name"})

        mock_make_request.assert_called_once_with(
            "label/U__app_2e_kubernetes_2e_io_2f_name/values", params=None
        )

@pytest.mark.asyncio
async def test_find_series(mock_make_request):
    """Test the find_series tool."""
    mock_make_request.return_value = [
        {"__name__": "up", "job": "prometheus", "instance": "localhost:9090"},
        {"__name__": "up", "job": "node", "instance": "localhost:9100"}
    ]

    async with Client(mcp) as client:
        result = await client.call_tool("find_series", {"match": ["up"]})

        mock_make_request.assert_called_once_with("series", params={"match[]": ["up"]})
        assert result.data["returned_count"] == 2
        assert result.data["has_more"] == False
        assert result.data["series"][0]["job"] == "prometheus"

@pytest.mark.asyncio
async def test_find_series_with_limit_and_time_range(mock_make_request):
    """Test the find_series tool forwards a server-side limit of limit+1 and time range."""
    mock_make_request.return_value = [
        {"__name__": "up", "job": "a"},
        {"__name__": "up", "job": "b"},
        {"__name__": "up", "job": "c"}
    ]

    async with Client(mcp) as client:
        result = await client.call_tool("find_series", {
            "match": ["up"],
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z",
            "limit": 2
        })

        mock_make_request.assert_called_once_with("series", params={
            "match[]": ["up"],
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z",
            "limit": 3
        })
        assert result.data["returned_count"] == 2
        assert result.data["has_more"] == True
        assert len(result.data["series"]) == 2

@pytest.mark.asyncio
async def test_find_series_requires_match(mock_make_request):
    """Test the find_series tool rejects an empty match list."""
    async with Client(mcp) as client:
        with pytest.raises(Exception, match="at least one series selector"):
            await client.call_tool("find_series", {"match": []})

        mock_make_request.assert_not_called()

@pytest.mark.asyncio
async def test_find_series_rejects_non_positive_limit(mock_make_request):
    """Test the find_series tool rejects zero and negative limits."""
    async with Client(mcp) as client:
        with pytest.raises(Exception, match="positive"):
            await client.call_tool("find_series", {"match": ["up"], "limit": 0})
        with pytest.raises(Exception, match="positive"):
            await client.call_tool("find_series", {"match": ["up"], "limit": -1})

        mock_make_request.assert_not_called()


# --- Runtime & build diagnostics tools ---

@pytest.mark.asyncio
async def test_get_runtime_info(mock_make_request):
    """Test the get_runtime_info tool."""
    mock_make_request.return_value = {
        "startTime": "2023-01-01T00:00:00Z",
        "CWD": "/prometheus",
        "reloadConfigSuccess": True,
        "goroutineCount": 42,
        "storageRetention": "15d"
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_runtime_info", {})

        mock_make_request.assert_called_once_with("status/runtimeinfo")
        assert result.data["goroutineCount"] == 42
        assert result.data["storageRetention"] == "15d"

@pytest.mark.asyncio
async def test_get_build_info(mock_make_request):
    """Test the get_build_info tool."""
    mock_make_request.return_value = {
        "version": "3.0.0",
        "revision": "abc123",
        "branch": "HEAD",
        "goVersion": "go1.23.0"
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_build_info", {})

        mock_make_request.assert_called_once_with("status/buildinfo")
        assert result.data["version"] == "3.0.0"
        assert result.data["goVersion"] == "go1.23.0"

@pytest.mark.asyncio
async def test_get_tsdb_stats(mock_make_request):
    """Test the get_tsdb_stats tool."""
    mock_make_request.return_value = {
        "headStats": {"numSeries": 508, "chunkCount": 937},
        "seriesCountByMetricName": [{"name": "net_conntrack_dialer_conn_failed_total", "value": 20}]
    }

    async with Client(mcp) as client:
        result = await client.call_tool("get_tsdb_stats", {})

        mock_make_request.assert_called_once_with("status/tsdb", params=None)
        assert result.data["headStats"]["numSeries"] == 508

@pytest.mark.asyncio
async def test_get_tsdb_stats_with_limit(mock_make_request):
    """Test the get_tsdb_stats tool forwards the limit parameter."""
    mock_make_request.return_value = {"headStats": {"numSeries": 508}}

    async with Client(mcp) as client:
        await client.call_tool("get_tsdb_stats", {"limit": 5})

        mock_make_request.assert_called_once_with("status/tsdb", params={"limit": 5})

@pytest.mark.asyncio
async def test_get_tsdb_stats_rejects_non_positive_limit(mock_make_request):
    """Test the get_tsdb_stats tool rejects limit < 1 before making a request."""
    async with Client(mcp) as client:
        with pytest.raises(Exception, match="positive"):
            await client.call_tool("get_tsdb_stats", {"limit": 0})

        mock_make_request.assert_not_called()


# --- Exemplars tool ---

@pytest.mark.asyncio
async def test_query_exemplars_basic(mock_make_request):
    """Test basic exemplar query returns expected structure with series_count and exemplar_count."""
    mock_make_request.return_value = [
        {
            "seriesLabels": {"__name__": "http_request_duration_seconds_bucket", "le": "0.5"},
            "exemplars": [
                {"labels": {"traceID": "abc123"}, "value": "0.4", "timestamp": 1617898400.123},
                {"labels": {"traceID": "def456"}, "value": "0.3", "timestamp": 1617898401.456},
            ]
        },
        {
            "seriesLabels": {"__name__": "http_request_duration_seconds_bucket", "le": "1.0"},
            "exemplars": [
                {"labels": {"traceID": "ghi789"}, "value": "0.8", "timestamp": 1617898402.789},
            ]
        }
    ]

    async with Client(mcp) as client:
        result = await client.call_tool("query_exemplars", {"query": "http_request_duration_seconds_bucket"})

        mock_make_request.assert_called_once_with(
            "query_exemplars", params={"query": "http_request_duration_seconds_bucket"}
        )
        assert result.data["series_count"] == 2
        assert result.data["exemplar_count"] == 3
        assert result.data["exemplars"] == mock_make_request.return_value


@pytest.mark.asyncio
async def test_query_exemplars_with_time_range(mock_make_request):
    """Test query with start/end forwards the parameters correctly."""
    mock_make_request.return_value = []

    async with Client(mcp) as client:
        await client.call_tool("query_exemplars", {
            "query": "http_request_duration_seconds_bucket",
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z",
        })

        mock_make_request.assert_called_once_with(
            "query_exemplars", params={
                "query": "http_request_duration_seconds_bucket",
                "start": "2023-01-01T00:00:00Z",
                "end": "2023-01-01T01:00:00Z",
            }
        )


@pytest.mark.asyncio
async def test_query_exemplars_empty_response(mock_make_request):
    """Test empty exemplar response returns exemplar_count: 0."""
    mock_make_request.return_value = []

    async with Client(mcp) as client:
        result = await client.call_tool("query_exemplars", {"query": "nonexistent_metric"})

        assert result.data["series_count"] == 0
        assert result.data["exemplar_count"] == 0
        assert result.data["exemplars"] == []


@pytest.mark.asyncio
async def test_query_exemplars_graceful_404(mock_make_request):
    """Test Prometheus returns 404 or empty list for versions without exemplar support."""
    mock_make_request.side_effect = Exception("HTTP 404: Not Found")

    async with Client(mcp) as client:
        result = await client.call_tool("query_exemplars", {"query": "some_metric"})

        assert result.data["series_count"] == 0
        assert result.data["exemplar_count"] == 0
        assert result.data["exemplars"] == []


# --- Format query tool ---

@pytest.mark.asyncio
async def test_format_query_valid(mock_make_request):
    """Test a valid query returns the formatted expression."""
    mock_make_request.return_value = 'up{job="prometheus"}'

    async with Client(mcp) as client:
        result = await client.call_tool("format_query", {"query": 'up{job="prometheus"}'})

        mock_make_request.assert_called_once_with("format_query", params={"query": 'up{job="prometheus"}'})
        assert result.data["formatted_query"] == 'up{job="prometheus"}'
        assert result.data["original_query"] == 'up{job="prometheus"}'
        assert result.data["is_same"] is True


@pytest.mark.asyncio
async def test_format_query_reformatted(mock_make_request):
    """Test a query with extra whitespace returns a cleaned version with is_same=False."""
    mock_make_request.return_value = 'up{job="prometheus"}'

    async with Client(mcp) as client:
        result = await client.call_tool("format_query", {"query": '  up { job = "prometheus" } '})

        mock_make_request.assert_called_once_with("format_query", params={"query": '  up { job = "prometheus" } '})
        assert result.data["formatted_query"] == 'up{job="prometheus"}'
        assert result.data["original_query"] == '  up { job = "prometheus" } '
        assert result.data["is_same"] is False


@pytest.mark.asyncio
async def test_format_query_already_canonical(mock_make_request):
    """Test an already-canonical query returns is_same=True."""
    mock_make_request.return_value = "rate(http_requests_total[5m])"

    async with Client(mcp) as client:
        result = await client.call_tool("format_query", {"query": "rate(http_requests_total[5m])"})

        assert result.data["is_same"] is True


@pytest.mark.asyncio
async def test_format_query_invalid_raises(mock_make_request):
    """Test an invalid PromQL expression raises ValueError with a descriptive error."""
    mock_make_request.side_effect = ValueError("Prometheus API error: 1:6: parse error: unexpected end of input")

    async with Client(mcp) as client:
        with pytest.raises(Exception, match="parse error"):
            await client.call_tool("format_query", {"query": "up{"})


# --- Target metadata tool ---

@pytest.mark.asyncio
async def test_get_target_metadata_no_params(mock_make_request):
    """Call without parameters returns all metadata with correct count."""
    mock_make_request.return_value = [
        {"target": {"instance": "localhost:9090", "job": "prometheus"}, "metric": "up", "type": "gauge", "help": "Up", "unit": ""},
        {"target": {"instance": "localhost:9100", "job": "node"}, "metric": "node_cpu_seconds_total", "type": "counter", "help": "CPU", "unit": "seconds"},
    ]

    async with Client(mcp) as client:
        result = await client.call_tool("get_target_metadata", {})

        mock_make_request.assert_called_once_with("targets/metadata", params=None)
        assert result.data["count"] == 2
        assert result.data["metadata"] == mock_make_request.return_value


@pytest.mark.asyncio
async def test_get_target_metadata_with_match_target(mock_make_request):
    """Call with match_target forwards the parameter correctly."""
    mock_make_request.return_value = []

    async with Client(mcp) as client:
        await client.call_tool("get_target_metadata", {"match_target": '{job="prometheus"}'})

        mock_make_request.assert_called_once_with("targets/metadata", params={"match_target": '{job="prometheus"}'})


@pytest.mark.asyncio
async def test_get_target_metadata_with_metric(mock_make_request):
    """Call with metric forwards the parameter correctly."""
    mock_make_request.return_value = []

    async with Client(mcp) as client:
        await client.call_tool("get_target_metadata", {"metric": "up"})

        mock_make_request.assert_called_once_with("targets/metadata", params={"metric": "up"})


@pytest.mark.asyncio
async def test_get_target_metadata_with_limit(mock_make_request):
    """Call with limit forwards the parameter correctly."""
    mock_make_request.return_value = []

    async with Client(mcp) as client:
        await client.call_tool("get_target_metadata", {"limit": 5})

        mock_make_request.assert_called_once_with("targets/metadata", params={"limit": 5})


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", [0, -1])
async def test_get_target_metadata_rejects_non_positive_limit(mock_make_request, bad_limit):
    """limit=0 or limit=-1 raises ValueError before making a request."""
    async with Client(mcp) as client:
        with pytest.raises(Exception, match="positive"):
            await client.call_tool("get_target_metadata", {"limit": bad_limit})

        mock_make_request.assert_not_called()


@pytest.mark.asyncio
async def test_get_target_metadata_empty_response(mock_make_request):
    """Empty response from Prometheus returns count: 0."""
    mock_make_request.return_value = []

    async with Client(mcp) as client:
        result = await client.call_tool("get_target_metadata", {"metric": "nonexistent"})

        assert result.data["count"] == 0
        assert result.data["metadata"] == []
