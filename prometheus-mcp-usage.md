# Prometheus MCP Server - Usage Skill

You are an expert in using the Prometheus MCP Server. This MCP server provides AI assistants with direct access to
Prometheus metrics, PromQL queries, and monitoring infrastructure through the Model Context Protocol.

## Server Overview

**Name:** Prometheus MCP Server **Version:** 1.6.2 **Repository:** https://github.com/pab1it0/prometheus-mcp-server
**License:** MIT **Python:** >= 3.10

The server wraps the Prometheus HTTP API v1 and exposes 20 MCP tools for querying metrics, discovering targets, managing
alerts, and inspecting server state.

---

## Configuration (Environment Variables)

### Required

| Variable         | Description              | Example                 |
|------------------|--------------------------|-------------------------|
| `PROMETHEUS_URL` | URL to Prometheus server | `http://localhost:9090` |

### Authentication (choose one method)

| Variable                                           | Description                                        |
|----------------------------------------------------|----------------------------------------------------|
| `PROMETHEUS_USERNAME` + `PROMETHEUS_PASSWORD`      | Basic authentication                               |
| `PROMETHEUS_TOKEN`                                 | Bearer token authentication                        |
| `PROMETHEUS_CLIENT_CERT` + `PROMETHEUS_CLIENT_KEY` | Mutual TLS (client certificate)                    |
| `REQUESTS_CA_BUNDLE`                               | CA bundle path for server certificate verification |

### Connection Options

| Variable                     | Default | Description                                                   |
|------------------------------|---------|---------------------------------------------------------------|
| `PROMETHEUS_URL_SSL_VERIFY`  | `True`  | Verify SSL certificates                                       |
| `PROMETHEUS_REQUEST_TIMEOUT` | `30`    | Request timeout in seconds                                    |
| `PROMETHEUS_DISABLE_LINKS`   | `False` | Disable Prometheus UI links in responses (saves tokens)       |
| `PROMETHEUS_CUSTOM_HEADERS`  | —       | Custom headers as JSON string, e.g. `'{"X-Custom": "value"}'` |

### Multi-Tenant

| Variable                               | Default | Description                                           |
|----------------------------------------|---------|-------------------------------------------------------|
| `ORG_ID`                               | —       | Default tenant ID sent as `X-Scope-OrgID` header      |
| `PROMETHEUS_MCP_ALLOW_ORG_ID_OVERRIDE` | `False` | Allow per-call `org_id` override when `ORG_ID` is set |

### Transport

| Variable                          | Default     | Description                                       |
|-----------------------------------|-------------|---------------------------------------------------|
| `PROMETHEUS_MCP_SERVER_TRANSPORT` | `stdio`     | Transport type: `stdio`, `http`, or `sse`         |
| `PROMETHEUS_MCP_BIND_HOST`        | `127.0.0.1` | Bind host for HTTP/SSE transports                 |
| `PROMETHEUS_MCP_BIND_PORT`        | `8080`      | Bind port for HTTP/SSE transports                 |
| `PROMETHEUS_MCP_STATELESS_HTTP`   | `False`     | Stateless HTTP mode for multi-replica deployments |

### Tool Management

| Variable                       | Default | Description                                                           |
|--------------------------------|---------|-----------------------------------------------------------------------|
| `PROMETHEUS_MCP_ENABLED_TOOLS` | all     | Comma-separated allowlist of tool base names to register              |
| `TOOL_PREFIX`                  | —       | Prefix for all tool names (e.g. `staging` -> `staging_execute_query`) |

### MCP 2026-07-28 Spec Features

| Variable                        | Default  | Description                              |
|---------------------------------|----------|------------------------------------------|
| `PROMETHEUS_MCP_SPEC_2026`      | `True`   | Enable 2026-07-28 compatibility layer    |
| `PROMETHEUS_MCP_CACHE_TTL_MS`   | `300000` | Cache TTL in milliseconds                |
| `PROMETHEUS_MCP_CACHE_SCOPE`    | `public` | Cache scope: `public` or `private`       |
| `PROMETHEUS_MCP_STRICT_HEADERS` | `False`  | Enforce strict `Mcp-*` header validation |

---

## All Tools Reference

### 1. `execute_query` - Execute PromQL Instant Query

Execute a PromQL instant query against Prometheus.

**Parameters:**

| Parameter | Type     | Required | Default      | Description                                      |
|-----------|----------|----------|--------------|--------------------------------------------------|
| `query`   | `string` | Yes      | —            | PromQL query string                              |
| `time`    | `string` | No       | current time | Evaluation timestamp (RFC3339 or Unix timestamp) |
| `org_id`  | `string` | No       | `""`         | Tenant ID sent as `X-Scope-OrgID` for this call  |

**Returns:** `{ resultType, result, links? }`

- `resultType`: `"vector"`, `"matrix"`, `"scalar"`, or `"string"`
- `result`: Array of query results
- `links`: (if not disabled) link to Prometheus UI

**Example usage:**

```
execute_query(query="up")
execute_query(query="rate(http_requests_total[5m])", time="2024-01-15T10:00:00Z")
execute_query(query="node_memory_MemAvailable_bytes", org_id="team-a")
```

---

### 2. `execute_range_query` - Execute PromQL Range Query

Execute a PromQL range query with start time, end time, and step interval.

**Parameters:**

| Parameter | Type     | Required | Default | Description                                          |
|-----------|----------|----------|---------|------------------------------------------------------|
| `query`   | `string` | Yes      | —       | PromQL query string                                  |
| `start`   | `string` | Yes      | —       | Start time (RFC3339 or Unix timestamp)               |
| `end`     | `string` | Yes      | —       | End time (RFC3339 or Unix timestamp)                 |
| `step`    | `string` | Yes      | —       | Resolution step width (e.g. `15s`, `1m`, `5m`, `1h`) |
| `org_id`  | `string` | No       | `""`    | Tenant ID sent as `X-Scope-OrgID`                    |

**Returns:** `{ resultType, result, links? }`

- `resultType`: Usually `"matrix"`
- `result`: Array with time-series data points

**Example usage:**

```
execute_range_query(
  query="rate(http_requests_total[5m])",
  start="2024-01-15T00:00:00Z",
  end="2024-01-15T12:00:00Z",
  step="1m"
)
```

**Note:** Supports MCP progress reporting for long-running queries.

---

### 3. `list_metrics` - List Available Metrics

List all available metric names in Prometheus with optional pagination and filtering.

**Parameters:**

| Parameter        | Type      | Required | Default | Description                                         |
|------------------|-----------|----------|---------|-----------------------------------------------------|
| `limit`          | `integer` | No       | all     | Maximum number of metrics to return                 |
| `offset`         | `integer` | No       | `0`     | Number of metrics to skip (pagination)              |
| `filter_pattern` | `string`  | No       | —       | Substring filter on metric names (case-insensitive) |
| `refresh_cache`  | `boolean` | No       | `false` | Force cache refresh (cache TTL is 5 min)            |

**Returns:** `{ metrics, total_count, returned_count, offset, has_more }`

**Example usage:**

```
list_metrics()
list_metrics(filter_pattern="http", limit=20)
list_metrics(offset=100, limit=50)
list_metrics(refresh_cache=true)
```

---

### 4. `get_metric_metadata` - Get Metric Metadata

Get metadata (type, help text, unit) for metrics. Supports single metric lookup and bulk search.

**Parameters:**

| Parameter        | Type      | Required | Default | Description                                                   |
|------------------|-----------|----------|---------|---------------------------------------------------------------|
| `metric`         | `string`  | No       | —       | Specific metric name (returns legacy list format)             |
| `filter_pattern` | `string`  | No       | —       | Substring filter on names and descriptions (case-insensitive) |
| `limit`          | `integer` | No       | all     | Max metrics in bulk mode                                      |
| `offset`         | `integer` | No       | `0`     | Skip count in bulk mode                                       |

**Returns:**

- If `metric` is provided: `List[{type, help, unit}]`
- If `metric` is omitted: `{ metadata, total_count, returned_count, offset, has_more }`

**Example usage:**

```
get_metric_metadata(metric="node_cpu_seconds_total")
get_metric_metadata(filter_pattern="request", limit=10)
get_metric_metadata()  # all metadata with pagination
```

---

### 5. `get_targets` - Get Scrape Targets

Get Prometheus scrape targets with server-side state/pool filtering and pagination.

**Parameters:**

| Parameter     | Type      | Required | Default    | Description                                                                                                          |
|---------------|-----------|----------|------------|----------------------------------------------------------------------------------------------------------------------|
| `state`       | `string`  | No       | `"active"` | Target state: `"active"`, `"dropped"`, `"any"`. Aliases: `"up"`->`"active"`, `"down"`->`"dropped"`, `"all"`->`"any"` |
| `scrape_pool` | `string`  | No       | —          | Filter by scrape pool name (server-side)                                                                             |
| `limit`       | `integer` | No       | all        | Max targets per category                                                                                             |
| `offset`      | `integer` | No       | `0`        | Skip count for pagination                                                                                            |

**Returns:**
`{ activeTargets, droppedTargets, total_active, total_dropped, returned_active, returned_dropped, offset, has_more, state }`

**Example usage:**

```
get_targets()
get_targets(state="dropped", scrape_pool="node-exporter")
get_targets(state="any", limit=10)
```

**Warning:** Avoid `state="any"` or `state="dropped"` without `scrape_pool` on large clusters - dropped targets can be
extremely large and may exhaust memory.

---

### 6. `list_alerts` - List Active Alerts

Get all active alerts from Prometheus with their state, labels, and annotations.

**Parameters:** None

**Returns:** `{ alerts, alert_count }`

- `alerts`: Array of alert objects with `labels`, `annotations`, `state`, `activeAt`

**Example usage:**

```
list_alerts()
```

---

### 7. `list_rules` - List Alerting & Recording Rules

Get alerting and recording rules with their health, state, and evaluation info.

**Parameters:**

| Parameter    | Type           | Required | Default | Description                                  |
|--------------|----------------|----------|---------|----------------------------------------------|
| `type`       | `string`       | No       | —       | Filter by rule type: `"alert"` or `"record"` |
| `rule_name`  | `List[string]` | No       | —       | Filter by rule names                         |
| `rule_group` | `List[string]` | No       | —       | Filter by rule group names                   |

**Returns:** `{ groups, group_count }`

**Example usage:**

```
list_rules()
list_rules(type="alert")
list_rules(rule_name=["HighMemoryUsage", "HighCPU"])
list_rules(rule_group=["node_alerts"], type="alert")
```

**Note:** Client-side filtering is re-applied as a fallback for Prometheus < 2.44 and compatible backends (Thanos,
VictoriaMetrics) that ignore `rule_name[]`/`rule_group[]`.

---

### 8. `list_label_names` - List Label Names

List all label names known to Prometheus, optionally restricted by series selectors and time range.

**Parameters:**

| Parameter | Type           | Required | Default | Description                            |
|-----------|----------------|----------|---------|----------------------------------------|
| `match`   | `List[string]` | No       | —       | Series selectors to restrict labels    |
| `start`   | `string`       | No       | —       | Start time (RFC3339 or Unix timestamp) |
| `end`     | `string`       | No       | —       | End time (RFC3339 or Unix timestamp)   |

**Returns:** `{ labels, count }`

**Example usage:**

```
list_label_names()
list_label_names(match=["up", 'node_cpu_seconds_total{job="node"}'])
list_label_names(start="2024-01-15T00:00:00Z", end="2024-01-15T12:00:00Z")
```

---

### 9. `list_label_values` - List Label Values

List all values for a specific label name.

**Parameters:**

| Parameter    | Type           | Required | Default | Description                                           |
|--------------|----------------|----------|---------|-------------------------------------------------------|
| `label_name` | `string`       | Yes      | —       | Label name (e.g. `"job"`, `"instance"`, `"__name__"`) |
| `match`      | `List[string]` | No       | —       | Series selectors to restrict values                   |
| `start`      | `string`       | No       | —       | Start time (RFC3339 or Unix timestamp)                |
| `end`        | `string`       | No       | —       | End time (RFC3339 or Unix timestamp)                  |

**Returns:** `{ values, count }`

**Example usage:**

```
list_label_values(label_name="job")
list_label_values(label_name="instance", match=['up{job="node"}'])
```

**Note:** Supports UTF-8 label names (Prometheus 3.x) with automatic escaping.

---

### 10. `find_series` - Find Time Series

Find time series matching label selectors.

**Parameters:**

| Parameter | Type           | Required | Default | Description                              |
|-----------|----------------|----------|---------|------------------------------------------|
| `match`   | `List[string]` | Yes      | —       | Series selectors (at least one required) |
| `start`   | `string`       | No       | —       | Start time (RFC3339 or Unix timestamp)   |
| `end`     | `string`       | No       | —       | End time (RFC3339 or Unix timestamp)     |
| `limit`   | `integer`      | No       | all     | Max series to return (must be positive)  |

**Returns:** `{ series, returned_count, has_more }`

**Example usage:**

```
find_series(match=['up{job="node"}'])
find_series(match=["http_requests_total"], limit=100)
find_series(match=['{__name__=~"node_.*"}'], start="2024-01-15T00:00:00Z", end="2024-01-15T12:00:00Z")
```

---

### 11. `get_runtime_info` - Get Runtime Information

Get Prometheus runtime information (start time, config reload status, goroutine count, storage retention).

**Parameters:** None

**Returns:** Runtime info dict with fields like `startTime`, `CWD`, `reloadConfigSuccess`, `lastConfigTime`,
`corruptionCount`, `goroutineCount`, `GOMAXPROCS`, `GOGC`, `GODEBUG`, `storageRetention`

**Example usage:**

```
get_runtime_info()
```

---

### 12. `get_build_info` - Get Build Information

Get Prometheus build information (version, revision, Go version, build date).

**Parameters:** None

**Returns:** Build info dict with `version`, `revision`, `branch`, `buildUser`, `buildDate`, `goVersion`

**Example usage:**

```
get_build_info()
```

---

### 13. `get_tsdb_stats` - Get TSDB Statistics

Get TSDB cardinality statistics: head series counts and top metrics by series count.

**Parameters:**

| Parameter | Type      | Required | Default | Description                                 |
|-----------|-----------|----------|---------|---------------------------------------------|
| `limit`   | `integer` | No       | `10`    | Max items per stats list (must be positive) |

**Returns:** TSDB stats dict with `headStats`, `seriesCountByMetricName`, `labelValueCountByLabelName`,
`memoryInBytesByLabelName`, `seriesCountByLabelValuePair`

**Example usage:**

```
get_tsdb_stats()
get_tsdb_stats(limit=20)
```

---

### 14. `query_exemplars` - Query Exemplars

Query exemplars (trace span references linked to metric samples). Requires Prometheus 2.26+.

**Parameters:**

| Parameter | Type     | Required | Default | Description                            |
|-----------|----------|----------|---------|----------------------------------------|
| `query`   | `string` | Yes      | —       | PromQL selector expression             |
| `start`   | `string` | No       | —       | Start time (RFC3339 or Unix timestamp) |
| `end`     | `string` | No       | —       | End time (RFC3339 or Unix timestamp)   |

**Returns:** `{ exemplars, series_count, exemplar_count }`

**Example usage:**

```
query_exemplars(query="http_request_duration_seconds_bucket")
query_exemplars(query="http_requests_total", start="2024-01-15T00:00:00Z", end="2024-01-15T12:00:00Z")
```

---

### 15. `format_query` - Format/Validate PromQL

Validate and pretty-print a PromQL expression.

**Parameters:**

| Parameter | Type     | Required | Default | Description                 |
|-----------|----------|----------|---------|-----------------------------|
| `query`   | `string` | Yes      | —       | PromQL expression to format |

**Returns:** `{ formatted_query, original_query, is_same }`

**Example usage:**

```
format_query(query="rate(http_requests_total[5m])")
format_query(query="sum by (job)(rate(http_requests_total[5m]))")
```

**Tip:** Use this to validate PromQL syntax before executing a query.

---

### 16. `get_target_metadata` - Get Per-Target Metadata

Get per-target metric metadata (type, help, unit) as reported by individual scrape targets. Disambiguates
identically-named metrics from different exporters.

**Parameters:**

| Parameter      | Type      | Required | Default | Description                                              |
|----------------|-----------|----------|---------|----------------------------------------------------------|
| `match_target` | `string`  | No       | —       | Label selector to filter targets (e.g. `'{job="node"}'`) |
| `metric`       | `string`  | No       | —       | Metric name to filter                                    |
| `limit`        | `integer` | No       | —       | Max entries to return (must be positive)                 |

**Returns:** `{ metadata, count }`

**Example usage:**

```
get_target_metadata()
get_target_metadata(match_target='{job="node"}', metric="node_cpu_seconds_total")
get_target_metadata(metric="up", limit=5)
```

---

### 17. `get_alertmanagers` - Get Alertmanager Discovery

Get current Alertmanager discovery state: which instances Prometheus sends alerts to.

**Parameters:** None

**Returns:** `{ activeAlertmanagers, droppedAlertmanagers, active_count, dropped_count }`

**Example usage:**

```
get_alertmanagers()
```

---

### 18. `get_prometheus_config` - Get Prometheus Configuration

Get the currently loaded Prometheus configuration as YAML.

**Parameters:** None

**Returns:** `{ yaml }`

**Example usage:**

```
get_prometheus_config()
```

**Warning:** Output may contain sensitive data on Prometheus versions prior to v2.19 (secrets not redacted).

---

### 19. `get_prometheus_flags` - Get Prometheus Flags

Get the command-line flags Prometheus was started with (retention period, storage path, admin API status, etc.).

**Parameters:** None

**Returns:** `{ flags, count }`

**Example usage:**

```
get_prometheus_flags()
```

---

### 20. `health_check` - Health Check

Health check endpoint for container monitoring and status verification.

**Parameters:** None

**Returns:**
`{ status, service, version, timestamp, transport, configuration, prometheus_connectivity?, prometheus_url?, prometheus_error? }`

**Example usage:**

```
health_check()
```

---

## Installation & Running

### Via uvx (recommended)

```bash
PROMETHEUS_URL=http://localhost:9090 uvx prometheus-mcp-server
```

### Via Docker

```bash
docker run -e PROMETHEUS_URL=http://host.docker.internal:9090 ghcr.io/pab1it0/prometheus-mcp-server:1.6.2
```

### Via pip

```bash
pip install prometheus-mcp-server
PROMETHEUS_URL=http://localhost:9090 prometheus-mcp-server
```

### MCP Client Configuration (Claude Desktop example)

```json
{
    "mcpServers": {
        "prometheus": {
            "command": "uvx",
            "args": [
                "prometheus-mcp-server"
            ],
            "env": {
                "PROMETHEUS_URL": "http://localhost:9090"
            }
        }
    }
}
```

### HTTP Transport Configuration

```json
{
    "mcpServers": {
        "prometheus": {
            "command": "uvx",
            "args": [
                "prometheus-mcp-server"
            ],
            "env": {
                "PROMETHEUS_URL": "http://localhost:9090",
                "PROMETHEUS_MCP_SERVER_TRANSPORT": "http",
                "PROMETHEUS_MCP_BIND_HOST": "0.0.0.0",
                "PROMETHEUS_MCP_BIND_PORT": "8080"
            }
        }
    }
}
```

---

## Common Workflows

### Investigating a service health issue

1. `health_check()` - verify MCP server connectivity
2. `execute_query(query="up{job='myservice'}")` - check if targets are up
3. `get_targets(scrape_pool="myservice")` - inspect scrape target details
4. `list_alerts()` - check active alerts
5. `execute_range_query(query="rate(http_requests_total{job='myservice'}[5m])", start=..., end=..., step="1m")` -
   analyze request rates

### Exploring available metrics

1. `list_metrics(filter_pattern="http")` - find HTTP-related metrics
2. `get_metric_metadata(metric="http_requests_total")` - get type/help/unit
3. `list_label_values(label_name="job")` - see all jobs
4. `find_series(match=['http_requests_total{job="api"}'])` - find specific series

### Capacity planning

1. `get_tsdb_stats()` - check cardinality
2. `get_runtime_info()` - check retention and storage
3. `get_prometheus_flags()` - check configuration flags
4. `execute_range_query(...)` - trend analysis queries

### Multi-tenant queries

Use `org_id` parameter on `execute_query` / `execute_range_query` to query specific tenants:

```
execute_query(query="up", org_id="team-backend")
```

Requires `PROMETHEUS_MCP_ALLOW_ORG_ID_OVERRIDE=true` if `ORG_ID` is already set globally.

---

## Key Features

- **Caching:** Metrics list is cached for 5 minutes (configurable via `PROMETHEUS_MCP_CACHE_TTL_MS`). Use
  `refresh_cache=true` on `list_metrics` to force refresh.
- **Pagination:** `list_metrics`, `get_metric_metadata`, `get_targets`, `find_series` support `limit`/`offset`pagination
  with `has_more` indicators.
- **Tool Filtering:** Use `PROMETHEUS_MCP_ENABLED_TOOLS` to expose only specific tools (reduces context window).
- **Tool Prefixing:** Use `TOOL_PREFIX` to namespace tools when running multiple instances (e.g.`staging_execute_query`,
  `prod_execute_query`).
- **UTF-8 Labels:** Automatic escaping for UTF-8 label names (Prometheus 3.x).
- **Progress Reporting:** Long-running operations (`execute_range_query`, `list_metrics`) report progress via MCP
  Context.
- **Prometheus UI Links:** Query results include clickable links to Prometheus UI (disable with
  `PROMETHEUS_DISABLE_LINKS=true`).
