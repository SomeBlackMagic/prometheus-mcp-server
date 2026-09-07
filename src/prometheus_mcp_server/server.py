#!/usr/bin/env python

import inspect
import os
import json
from typing import Annotated, Any, Dict, List, Optional, Union
from dataclasses import dataclass
import time
from datetime import datetime, timedelta
from enum import Enum

import dotenv
import requests
from fastmcp import FastMCP, Context
from pydantic import Field
from prometheus_mcp_server.logging_config import get_logger
from prometheus_mcp_server.spec2026.headers import is_safe_plain_ascii
from prometheus_mcp_server.spec2026.otel import get_trace_headers

dotenv.load_dotenv()

# Get tool prefix from environment (empty string for backward compatibility)
TOOL_PREFIX = os.environ.get("TOOL_PREFIX", "")

def _tool_name(name: str) -> str:
    """Build tool name with optional prefix."""
    return f"{TOOL_PREFIX}_{name}" if TOOL_PREFIX else name

# Optional server-side tool allowlist. When PROMETHEUS_MCP_ENABLED_TOOLS is set
# to a comma-separated list of tool base names (e.g.
# "execute_query,list_metrics"), only those tools are registered. When unset or
# empty, all tools are registered (backward compatible).
_enabled_tools_raw = os.environ.get("PROMETHEUS_MCP_ENABLED_TOOLS", "").strip()
ENABLED_TOOLS: Optional[set] = (
    {name.strip().lower() for name in _enabled_tools_raw.split(",") if name.strip()}
    if _enabled_tools_raw
    else None
)

def _tool_enabled(name: str) -> bool:
    """Return True when the given (unprefixed) tool name should be registered."""
    return ENABLED_TOOLS is None or name.lower() in ENABLED_TOOLS

# Include prefix in MCP server name if set
mcp_name = f"Prometheus MCP ({TOOL_PREFIX})" if TOOL_PREFIX else "Prometheus MCP"
mcp = FastMCP(mcp_name)

def _tool(*, name: str, **kwargs):
    """Conditional ``mcp.tool`` decorator that honours PROMETHEUS_MCP_ENABLED_TOOLS.

    When the tool is disabled we return a no-op decorator so the underlying
    coroutine is left undefined for the MCP server, effectively removing it
    from the surface.
    """
    base_name = name[len(TOOL_PREFIX) + 1:] if TOOL_PREFIX and name.startswith(f"{TOOL_PREFIX}_") else name
    if _tool_enabled(base_name):
        return mcp.tool(name=name, **kwargs)
    def _skip(func):
        return func
    return _skip

from starlette.requests import Request
from starlette.responses import JSONResponse

@mcp.custom_route("/health", methods=["GET"])
async def health_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})

# Cache for metrics list to improve completion performance
_metrics_cache = {"data": None, "timestamp": 0}
_CACHE_TTL = 300  # 5 minutes

def clear_metrics_cache():
    """Reset the metrics cache, forcing the next fetch to hit Prometheus."""
    _metrics_cache["data"] = None
    _metrics_cache["timestamp"] = 0

# Get logger instance
logger = get_logger()

# Health check tool for Docker containers and monitoring
@_tool(
    name=_tool_name("health_check"),
    description="Health check endpoint for container monitoring and status verification",
    annotations={
        "title": "Health Check",
        "icon": "❤️",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def health_check() -> Dict[str, Any]:
    """Return health status of the MCP server and Prometheus connection.

    Returns:
        Health status including service information, configuration, and connectivity
    """
    try:
        health_status = {
            "status": "healthy",
            "service": "prometheus-mcp-server",
            "version": "1.6.2",
            "timestamp": datetime.utcnow().isoformat(),
            "transport": config.mcp_server_config.mcp_server_transport if config.mcp_server_config else "stdio",
            "configuration": {
                "prometheus_url_configured": bool(config.url),
                "authentication_configured": bool(config.username or config.token or config.client_cert),
                "org_id_configured": bool(config.org_id)
            }
        }
        
        # Test Prometheus connectivity if configured
        if config.url:
            try:
                # Quick connectivity test
                make_prometheus_request("query", params={"query": "up", "time": str(int(time.time()))})
                health_status["prometheus_connectivity"] = "healthy"
                health_status["prometheus_url"] = config.url
            except Exception as e:
                health_status["prometheus_connectivity"] = "unhealthy"
                health_status["prometheus_error"] = str(e)
                health_status["status"] = "degraded"
        else:
            health_status["status"] = "unhealthy"
            health_status["error"] = "PROMETHEUS_URL not configured"
        
        logger.info("Health check completed", status=health_status["status"])
        return health_status
        
    except Exception as e:
        logger.error("Health check failed", error=str(e))
        return {
            "status": "unhealthy",
            "service": "prometheus-mcp-server",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


class TransportType(str, Enum):
    """Supported MCP server transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        """Get all valid transport values."""
        return [transport.value for transport in cls]

@dataclass
class MCPServerConfig:
    """Global Configuration for MCP."""
    mcp_server_transport: TransportType = None
    mcp_bind_host: str = None
    mcp_bind_port: int = None
    stateless_http: bool = False

    def __post_init__(self):
        """Validate mcp configuration."""
        if not self.mcp_server_transport:
            raise ValueError("MCP SERVER TRANSPORT is required")
        if not self.mcp_bind_host:
            raise ValueError(f"MCP BIND HOST is required")
        if not self.mcp_bind_port:
            raise ValueError(f"MCP BIND PORT is required")

@dataclass
class PrometheusConfig:
    url: str
    url_ssl_verify: bool = True
    disable_prometheus_links: bool = False
    # Optional credentials
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    # Optional Org ID for multi-tenant setups
    org_id: Optional[str] = None
    # Optional client TLS certificate for mutual TLS authentication
    client_cert: Optional[str] = None
    client_key: Optional[str] = None
    # Optional Custom MCP Server Configuration
    mcp_server_config: Optional[MCPServerConfig] = None
    # Optional custom headers for Prometheus requests
    custom_headers: Optional[Dict[str, str]] = None
    # Request timeout in seconds to prevent hanging requests (DDoS protection)
    request_timeout: int = 30
    # MCP 2026-07-28 compatibility layer (see prometheus_mcp_server.spec2026)
    spec_2026_enabled: bool = True
    cache_ttl_ms: int = 300000
    cache_scope: str = "public"
    strict_headers: bool = False
    allow_org_id_override: bool = False


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable without ever failing at import."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid integer environment variable, using default",
            variable=name,
            value=raw,
            default=default,
        )
        return default


config = PrometheusConfig(
    url=os.environ.get("PROMETHEUS_URL", ""),
    url_ssl_verify=os.environ.get("PROMETHEUS_URL_SSL_VERIFY", "True").lower() in ("true", "1", "yes"),
    disable_prometheus_links=os.environ.get("PROMETHEUS_DISABLE_LINKS", "False").lower() in ("true", "1", "yes"),
    username=os.environ.get("PROMETHEUS_USERNAME", ""),
    password=os.environ.get("PROMETHEUS_PASSWORD", ""),
    token=os.environ.get("PROMETHEUS_TOKEN", ""),
    org_id=os.environ.get("ORG_ID", ""),
    mcp_server_config=MCPServerConfig(
        mcp_server_transport=os.environ.get("PROMETHEUS_MCP_SERVER_TRANSPORT", "stdio").lower(),
        mcp_bind_host=os.environ.get("PROMETHEUS_MCP_BIND_HOST", "127.0.0.1"),
        mcp_bind_port=int(os.environ.get("PROMETHEUS_MCP_BIND_PORT", "8080")),
        stateless_http=os.environ.get("PROMETHEUS_MCP_STATELESS_HTTP", "False").lower() in ("true", "1", "yes"),
    ),
    client_cert=os.environ.get("PROMETHEUS_CLIENT_CERT", "") or None,
    client_key=os.environ.get("PROMETHEUS_CLIENT_KEY", "") or None,
    custom_headers=json.loads(os.environ.get("PROMETHEUS_CUSTOM_HEADERS")) if os.environ.get("PROMETHEUS_CUSTOM_HEADERS") else None,
    request_timeout=int(os.environ.get("PROMETHEUS_REQUEST_TIMEOUT", "30")),
    spec_2026_enabled=os.environ.get("PROMETHEUS_MCP_SPEC_2026", "True").lower() in ("true", "1", "yes"),
    cache_ttl_ms=_env_int("PROMETHEUS_MCP_CACHE_TTL_MS", 300000),
    cache_scope=os.environ.get("PROMETHEUS_MCP_CACHE_SCOPE", "public").lower(),
    strict_headers=os.environ.get("PROMETHEUS_MCP_STRICT_HEADERS", "False").lower() in ("true", "1", "yes"),
    allow_org_id_override=os.environ.get("PROMETHEUS_MCP_ALLOW_ORG_ID_OVERRIDE", "False").lower() in ("true", "1", "yes"),
)

def get_prometheus_auth():
    """Get authentication for Prometheus based on provided credentials."""
    if config.token:
        return {"Authorization": f"Bearer {config.token}"}
    elif config.username and config.password:
        return requests.auth.HTTPBasicAuth(config.username, config.password)
    return None

def resolve_org_id(org_id: Optional[str]) -> Optional[str]:
    """Decide which tenant id, if any, belongs on the outbound request.

    An operator-configured ORG_ID always wins over a per-call value unless the
    operator additionally set PROMETHEUS_MCP_ALLOW_ORG_ID_OVERRIDE.
    """
    configured = config.org_id or None
    if not org_id:
        return configured

    if not isinstance(org_id, str) or not is_safe_plain_ascii(org_id):
        logger.warning(
            "Rejecting unsafe per-call org_id; falling back to the configured tenant",
            org_id_type=type(org_id).__name__,
        )
        return configured

    if configured and not config.allow_org_id_override:
        logger.warning(
            "Ignoring per-call org_id because ORG_ID is configured",
            switch="PROMETHEUS_MCP_ALLOW_ORG_ID_OVERRIDE",
        )
        return configured

    logger.info("Using per-call tenant for this Prometheus request", org_id=org_id)
    return org_id


def make_prometheus_request(endpoint, params=None, org_id=None):
    """Make a request to the Prometheus API with proper authentication and headers."""
    if not config.url:
        logger.error("Prometheus configuration missing", error="PROMETHEUS_URL not set")
        raise ValueError("Prometheus configuration is missing. Please set PROMETHEUS_URL environment variable.")
    if not config.url_ssl_verify:
        logger.warning("SSL certificate verification is disabled. This is insecure and should not be used in production environments.", endpoint=endpoint)

    url = f"{config.url.rstrip('/')}/api/v1/{endpoint}"
    url_ssl_verify = config.url_ssl_verify
    auth = get_prometheus_auth()
    headers = {}

    if isinstance(auth, dict):  # Token auth is passed via headers
        headers.update(auth)
        auth = None  # Clear auth for requests.get if it's already in headers
    
    # Add OrgID header if specified. An operator-configured ORG_ID always wins
    # over a per-call value unless the operator opted into overrides.
    effective_org_id = resolve_org_id(org_id)
    if effective_org_id:
        headers["X-Scope-OrgID"] = effective_org_id

    # W3C trace context from the incoming request's _meta (2026-07-28).
    headers.update(get_trace_headers())

    if config.custom_headers:
        headers.update(config.custom_headers)

    # Build client certificate tuple for mutual TLS authentication
    client_cert = None
    if config.client_cert:
        if config.client_key:
            client_cert = (config.client_cert, config.client_key)
        else:
            client_cert = config.client_cert

    try:
        logger.debug("Making Prometheus API request", endpoint=endpoint, url=url, params=params, headers=headers, timeout=config.request_timeout)

        # Make the request with appropriate headers, auth, and timeout (DDoS protection)
        response = requests.get(url, params=params, auth=auth, headers=headers, verify=url_ssl_verify, cert=client_cert, timeout=config.request_timeout)

        response.raise_for_status()
        result = response.json()
        
        if result["status"] != "success":
            error_msg = result.get('error', 'Unknown error')
            logger.error("Prometheus API returned error", endpoint=endpoint, error=error_msg, status=result["status"])
            raise ValueError(f"Prometheus API error: {error_msg}")
        
        data_field = result.get("data", {})
        if isinstance(data_field, dict):
            result_type = data_field.get("resultType")
        else:
            result_type = "list"
        logger.debug("Prometheus API request successful", endpoint=endpoint, result_type=result_type)
        return result["data"]
    
    except requests.exceptions.RequestException as e:
        logger.error("HTTP request to Prometheus failed", endpoint=endpoint, url=url, error=str(e), error_type=type(e).__name__)
        raise
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Prometheus response as JSON", endpoint=endpoint, url=url, error=str(e))
        raise ValueError(f"Invalid JSON response from Prometheus: {str(e)}")
    except Exception as e:
        logger.error("Unexpected error during Prometheus request", endpoint=endpoint, url=url, error=str(e), error_type=type(e).__name__)
        raise

def get_cached_metrics() -> List[str]:
    """Get metrics list with caching to improve completion performance.

    This helper function is available for future completion support when
    FastMCP implements the completion capability. For now, it can be used
    internally to optimize repeated metric list requests.
    """
    current_time = time.time()

    # snapshot for clarity
    cached_data = _metrics_cache["data"]
    cached_timestamp = _metrics_cache["timestamp"]

    if cached_data is not None and (current_time - cached_timestamp) < _CACHE_TTL:
        logger.debug("Using cached metrics list", cache_age=current_time - cached_timestamp)
        return cached_data

    # Fetch fresh metrics
    data = make_prometheus_request("label/__name__/values")
    _metrics_cache["data"] = data
    _metrics_cache["timestamp"] = current_time
    logger.debug("Refreshed metrics cache", metric_count=len(data))
    return data

# Note: Argument completions will be added when FastMCP supports the completion
# capability. The get_cached_metrics() function above is ready for that integration.

def _org_id_json_schema(schema: Dict[str, Any]) -> None:
    """Attach the 2026-07-28 x-mcp-header annotation to the org_id property."""
    schema.pop("anyOf", None)
    schema.pop("default", None)
    schema["type"] = "string"
    schema["x-mcp-header"] = "Org-Id"


OrgIdParam = Annotated[
    Optional[str],
    Field(
        description=(
            "Tenant id sent as X-Scope-OrgID. Ignored when the server has ORG_ID "
            "configured, unless the operator enabled PROMETHEUS_MCP_ALLOW_ORG_ID_OVERRIDE"
        ),
        json_schema_extra=_org_id_json_schema,
    ),
]


@_tool(
    name=_tool_name("execute_query"),
    description="Execute a PromQL instant query against Prometheus",
    annotations={
        "title": "Execute PromQL Query",
        "icon": "📊",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def execute_query(query: str, time: Optional[str] = None, org_id: OrgIdParam = "") -> Dict[str, Any]:
    """Execute an instant query against Prometheus.

    Args:
        query: PromQL query string
        time: Optional RFC3339 or Unix timestamp (default: current time)
        org_id: Optional tenant id sent as X-Scope-OrgID for this call only.

    Returns:
        Query result with type (vector, matrix, scalar, string) and values
    """
    params = {"query": query}
    if time:
        params["time"] = time

    logger.info("Executing instant query", query=query, time=time, org_id=org_id)
    tenant_kwargs = {"org_id": org_id} if org_id else {}
    data = make_prometheus_request("query", params=params, **tenant_kwargs)

    result = {
        "resultType": data["resultType"],
        "result": data["result"]
    }

    if not config.disable_prometheus_links:
        from urllib.parse import urlencode
        ui_params = {"g0.expr": query, "g0.tab": "0"}
        if time:
            ui_params["g0.moment_input"] = time
        prometheus_ui_link = f"{config.url.rstrip('/')}/graph?{urlencode(ui_params)}"
        result["links"] = [{
            "href": prometheus_ui_link,
            "rel": "prometheus-ui",
            "title": "View in Prometheus UI"
        }]

    logger.info("Instant query completed",
                query=query,
                result_type=data["resultType"],
                result_count=len(data["result"]) if isinstance(data["result"], list) else 1)

    return result

@_tool(
    name=_tool_name("execute_range_query"),
    description="Execute a PromQL range query with start time, end time, and step interval",
    annotations={
        "title": "Execute PromQL Range Query",
        "icon": "📈",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def execute_range_query(query: str, start: str, end: str, step: str, ctx: Context | None = None, org_id: OrgIdParam = "") -> Dict[str, Any]:
    """Execute a range query against Prometheus.

    Args:
        query: PromQL query string
        start: Start time as RFC3339 or Unix timestamp
        end: End time as RFC3339 or Unix timestamp
        step: Query resolution step width (e.g., '15s', '1m', '1h')

    Returns:
        Range query result with type (usually matrix) and values over time
    """
    params = {
        "query": query,
        "start": start,
        "end": end,
        "step": step
    }

    logger.info("Executing range query", query=query, start=start, end=end, step=step, org_id=org_id)

    # Report progress if context available
    if ctx:
        await ctx.report_progress(progress=0, total=100, message="Initiating range query...")

    tenant_kwargs = {"org_id": org_id} if org_id else {}
    data = make_prometheus_request("query_range", params=params, **tenant_kwargs)

    # Report progress
    if ctx:
        await ctx.report_progress(progress=50, total=100, message="Processing query results...")

    result = {
        "resultType": data["resultType"],
        "result": data["result"]
    }

    if not config.disable_prometheus_links:
        from urllib.parse import urlencode
        ui_params = {
            "g0.expr": query,
            "g0.tab": "0",
            "g0.range_input": f"{start} to {end}",
            "g0.step_input": step
        }
        prometheus_ui_link = f"{config.url.rstrip('/')}/graph?{urlencode(ui_params)}"
        result["links"] = [{
            "href": prometheus_ui_link,
            "rel": "prometheus-ui",
            "title": "View in Prometheus UI"
        }]

    # Report completion
    if ctx:
        await ctx.report_progress(progress=100, total=100, message="Range query completed")

    logger.info("Range query completed",
                query=query,
                result_type=data["resultType"],
                result_count=len(data["result"]) if isinstance(data["result"], list) else 1)

    return result

@_tool(
    name=_tool_name("list_metrics"),
    description="List all available metrics in Prometheus with optional pagination support",
    annotations={
        "title": "List Available Metrics",
        "icon": "📋",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def list_metrics(
    limit: Optional[int] = None,
    offset: int = 0,
    filter_pattern: Optional[str] = None,
    ctx: Context | None = None,
    refresh_cache: bool = False,
) -> Dict[str, Any]:
    """Retrieve a list of all metric names available in Prometheus.

    Args:
        limit: Maximum number of metrics to return (default: all metrics)
        offset: Number of metrics to skip for pagination (default: 0)
        filter_pattern: Optional substring to filter metric names (case-insensitive)
        refresh_cache: Force a cache refresh to pick up newly scraped metrics (default: False)

    Returns:
        Dictionary containing:
        - metrics: List of metric names
        - total_count: Total number of metrics (before pagination)
        - returned_count: Number of metrics returned
        - offset: Current offset
        - has_more: Whether more metrics are available
    """
    logger.info("Listing available metrics", limit=limit, offset=offset, filter_pattern=filter_pattern, refresh_cache=refresh_cache)

    # Report progress if context available
    if ctx:
        await ctx.report_progress(progress=0, total=100, message="Fetching metrics list...")

    if refresh_cache:
        clear_metrics_cache()

    data = get_cached_metrics()

    if ctx:
        await ctx.report_progress(progress=50, total=100, message=f"Processing {len(data)} metrics...")

    # Apply filter if provided
    if filter_pattern:
        filtered_data = [m for m in data if filter_pattern.lower() in m.lower()]
        logger.debug("Applied filter", original_count=len(data), filtered_count=len(filtered_data), pattern=filter_pattern)
        data = filtered_data

    total_count = len(data)

    # Apply pagination
    start_idx = offset
    end_idx = offset + limit if limit is not None else len(data)
    paginated_data = data[start_idx:end_idx]

    result = {
        "metrics": paginated_data,
        "total_count": total_count,
        "returned_count": len(paginated_data),
        "offset": offset,
        "has_more": end_idx < total_count
    }

    if ctx:
        await ctx.report_progress(progress=100, total=100, message=f"Retrieved {len(paginated_data)} of {total_count} metrics")

    logger.info("Metrics list retrieved",
                total_count=total_count,
                returned_count=len(paginated_data),
                offset=offset,
                has_more=result["has_more"])

    return result

def _coerce_metadata_entries(value: Any) -> List[Dict[str, Any]]:
    """Normalize metadata value into a list of metadata dictionaries."""
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _normalize_metadata_map(raw_data: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Normalize diverse metadata response shapes into {metric_name: [entries]}."""
    if isinstance(raw_data, dict):
        if "metadata" in raw_data:
            return _normalize_metadata_map(raw_data["metadata"])
        if "data" in raw_data:
            return _normalize_metadata_map(raw_data["data"])

        normalized: Dict[str, List[Dict[str, Any]]] = {}
        for metric_name, entries in raw_data.items():
            if not isinstance(metric_name, str):
                continue
            coerced_entries = _coerce_metadata_entries(entries)
            if coerced_entries:
                normalized[metric_name] = coerced_entries

        if normalized:
            return normalized

        metric_name = raw_data.get("metric")
        if isinstance(metric_name, str):
            return {metric_name: [raw_data]}

    if isinstance(raw_data, list):
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for entry in raw_data:
            if not isinstance(entry, dict):
                continue
            metric_name = entry.get("metric")
            if not isinstance(metric_name, str):
                continue
            grouped.setdefault(metric_name, []).append(entry)
        return grouped

    return {}


def _metadata_matches_pattern(metric_name: str, entries: List[Dict[str, Any]], pattern: str) -> bool:
    """Return True when pattern matches metric name or metadata text fields."""
    lowered_pattern = pattern.lower()
    if lowered_pattern in metric_name.lower():
        return True

    for entry in entries:
        for value in entry.values():
            if isinstance(value, str) and lowered_pattern in value.lower():
                return True

    return False


@_tool(
    name=_tool_name("get_metric_metadata"),
    description=(
        "Get metadata (type, help, unit) for metrics. "
        "Returns all metric metadata when no metric name is provided. "
        "Use filter_pattern to search metric names and descriptions."
    ),
    annotations={
        "title": "Get Metric Metadata",
        "icon": "ℹ️",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def get_metric_metadata(
    metric: Optional[str] = None,
    filter_pattern: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """Get metadata for one metric or bulk metadata for all metrics.

    Args:
        metric: Optional metric name. If provided, returns legacy list format.
        filter_pattern: Optional substring filter on metric name and descriptions.
        limit: Maximum number of metrics to return in bulk mode.
        offset: Number of metrics to skip in bulk mode.

    Returns:
        If metric is provided: list of metadata entries for that metric.
        If metric is not provided: dict with filtered metadata and pagination info.
    """
    logger.info("Retrieving metric metadata", metric=metric, filter_pattern=filter_pattern, limit=limit, offset=offset)

    params = {"metric": metric} if metric else None
    raw_data = make_prometheus_request("metadata", params=params)

    metadata_by_metric = _normalize_metadata_map(raw_data)

    # Fallback for atypical single-metric response formats.
    if metric and metric not in metadata_by_metric:
        fallback_entries = _coerce_metadata_entries(raw_data)
        if fallback_entries:
            metadata_by_metric[metric] = fallback_entries

    if filter_pattern:
        metadata_by_metric = {
            metric_name: entries
            for metric_name, entries in metadata_by_metric.items()
            if _metadata_matches_pattern(metric_name, entries, filter_pattern)
        }

    if metric:
        metric_entries = metadata_by_metric.get(metric, [])
        logger.info("Metric metadata retrieved", metric=metric, metadata_count=len(metric_entries))
        return metric_entries

    metric_names = list(metadata_by_metric.keys())
    total_count = len(metric_names)
    start_idx = offset
    end_idx = offset + limit if limit is not None else total_count
    selected_metric_names = metric_names[start_idx:end_idx]
    paginated_metadata = {name: metadata_by_metric[name] for name in selected_metric_names}

    result = {
        "metadata": paginated_metadata,
        "total_count": total_count,
        "returned_count": len(paginated_metadata),
        "offset": offset,
        "has_more": end_idx < total_count,
    }

    logger.info(
        "Bulk metric metadata retrieved",
        total_count=total_count,
        returned_count=result["returned_count"],
        offset=offset,
        has_more=result["has_more"],
    )

    return result

@_tool(
    name=_tool_name("get_targets"),
    description="Get scrape targets, with state/pool filtering and optional pagination",
    annotations={
        "title": "Get Scrape Targets",
        "icon": "🎯",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def get_targets(
    state: str = "active",
    scrape_pool: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    """Get information about Prometheus scrape targets.

    Args:
        state: Which targets to request from Prometheus - "active" (default),
            "dropped", or "any". This is applied server-side by Prometheus.
        scrape_pool: Optional scrape pool name to restrict results to, applied
            server-side by Prometheus.
        limit: Maximum number of targets to return per category (default: all)
        offset: Number of targets to skip for pagination (default: 0)

    Returns:
        Dictionary containing:
        - activeTargets / droppedTargets: Target lists (after pagination)
        - total_active / total_dropped: Totals before pagination
        - returned_active / returned_dropped: Counts actually returned
        - offset: Current offset
        - has_more: Whether more targets are available
        - state: The state filter that was applied

    Note:
        The default is "active" rather than "any" deliberately. On a cluster of
        any size, service discovery finds far more targets than relabeling
        keeps, and each dropped target carries its full discoveredLabels map.
        Requesting dropped targets unfiltered can produce a response large
        enough to exhaust the server's memory before any pagination could be
        applied, since the whole payload is parsed first. Pass state="any" or
        "dropped" explicitly when you need them, ideally with scrape_pool.
    """
    valid_states = ("active", "dropped", "any")
    if state not in valid_states:
        raise ValueError(f"Invalid state '{state}'. Must be one of: {', '.join(valid_states)}")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")

    logger.info("Retrieving scrape targets information",
                state=state, scrape_pool=scrape_pool, limit=limit, offset=offset)

    # Filter server-side so Prometheus never sends what we do not need.
    params: Dict[str, str] = {}
    if state != "any":
        params["state"] = state
    if scrape_pool:
        params["scrapePool"] = scrape_pool

    data = make_prometheus_request("targets", params=params or None)

    active = data.get("activeTargets", []) or []
    dropped = data.get("droppedTargets", []) or []
    total_active, total_dropped = len(active), len(dropped)

    if limit is not None:
        end = offset + limit
        active, dropped = active[offset:end], dropped[offset:end]
    elif offset:
        active, dropped = active[offset:], dropped[offset:]

    result = {
        "activeTargets": active,
        "droppedTargets": dropped,
        "total_active": total_active,
        "total_dropped": total_dropped,
        "returned_active": len(active),
        "returned_dropped": len(dropped),
        "offset": offset,
        "has_more": offset + len(active) < total_active or offset + len(dropped) < total_dropped,
        "state": state,
    }

    logger.info("Scrape targets retrieved",
                active_targets=total_active,
                dropped_targets=total_dropped,
                returned_active=len(active),
                returned_dropped=len(dropped))

    return result

@_tool(
    name=_tool_name("list_alerts"),
    description="Get all active alerts from Prometheus with their state, labels, and annotations",
    annotations={
        "title": "List Active Alerts",
        "icon": "🚨",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def list_alerts() -> Dict[str, Any]:
    """Get all currently active alerts from Prometheus.

    Returns:
        Dictionary containing:
        - alerts: List of active alerts with labels, annotations, state, and activeAt
        - alert_count: Number of active alerts
    """
    logger.info("Retrieving active alerts")
    data = make_prometheus_request("alerts", params=None)

    alerts = data.get("alerts", [])
    result = {
        "alerts": alerts,
        "alert_count": len(alerts)
    }

    logger.info("Active alerts retrieved", alert_count=len(alerts))
    return result

@_tool(
    name=_tool_name("list_rules"),
    description="Get alerting and recording rules with their health, state, and evaluation info",
    annotations={
        "title": "List Alerting & Recording Rules",
        "icon": "📜",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def list_rules(
    type: Optional[str] = None,
    rule_name: Optional[List[str]] = None,
    rule_group: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Get alerting and recording rules currently loaded in Prometheus.

    Args:
        type: Optional rule type filter, either 'alert' or 'record'
        rule_name: Optional list of rule names to filter by (forwarded to the server
            on Prometheus >= 2.44 and re-applied client-side for older or
            Prometheus-compatible backends that ignore the parameter)
        rule_group: Optional list of rule group names to filter by (same fallback)

    Returns:
        Dictionary containing:
        - groups: List of rule groups with their rules
        - group_count: Number of rule groups returned
    """
    if type is not None and type not in ("alert", "record"):
        raise ValueError(f"Invalid rule type: '{type}'. Must be 'alert' or 'record'.")

    logger.info("Retrieving rules", type=type, rule_name=rule_name, rule_group=rule_group)

    params: Dict[str, Any] = {}
    if type:
        params["type"] = type
    if rule_name:
        params["rule_name[]"] = rule_name
    if rule_group:
        params["rule_group[]"] = rule_group

    data = make_prometheus_request("rules", params=params or None)

    groups = data.get("groups", [])
    # Prometheus < 2.44 and some compatible backends (Thanos, VictoriaMetrics) silently
    # ignore rule_name[]/rule_group[], so re-apply the filters client-side.
    if rule_group:
        wanted_groups = set(rule_group)
        groups = [g for g in groups if g.get("name") in wanted_groups]
    if rule_name:
        wanted_rules = set(rule_name)
        groups = [
            {**g, "rules": [r for r in g.get("rules", []) if r.get("name") in wanted_rules]}
            for g in groups
        ]
        groups = [g for g in groups if g["rules"]]

    result = {
        "groups": groups,
        "group_count": len(groups)
    }

    logger.info("Rules retrieved", group_count=len(groups))
    return result

@_tool(
    name=_tool_name("list_label_names"),
    description="List all label names, optionally restricted to series matching selectors and a time range",
    annotations={
        "title": "List Label Names",
        "icon": "🏷️",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def list_label_names(
    match: Optional[List[str]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Any]:
    """List label names known to Prometheus.

    Args:
        match: Optional list of series selectors (e.g. ['up', 'node_cpu_seconds_total{job="node"}'])
        start: Optional start time as RFC3339 or Unix timestamp
        end: Optional end time as RFC3339 or Unix timestamp

    Returns:
        Dictionary containing:
        - labels: List of label names
        - count: Number of label names returned
    """
    logger.info("Listing label names", match=match, start=start, end=end)

    params: Dict[str, Any] = {}
    if match:
        params["match[]"] = match
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    data = make_prometheus_request("labels", params=params or None)

    result = {
        "labels": data,
        "count": len(data)
    }

    logger.info("Label names retrieved", count=len(data))
    return result

def _is_legacy_label_rune(ch: str, index: int) -> bool:
    """Check whether a character is valid in a classic Prometheus label name."""
    return (
        ch == "_"
        or "a" <= ch <= "z"
        or "A" <= ch <= "Z"
        or (index > 0 and "0" <= ch <= "9")
    )


def _escape_label_name(name: str) -> str:
    """Escape a label name for use in a URL path using Prometheus 'values' escaping.

    Names valid under the classic charset ([a-zA-Z_][a-zA-Z0-9_]*) pass through
    unchanged. UTF-8 names (legal since Prometheus 3.x) are escaped to the U__ form
    the API requires for path segments.
    """
    if all(_is_legacy_label_rune(ch, i) for i, ch in enumerate(name)):
        return name

    escaped = ["U__"]
    for index, ch in enumerate(name):
        if ch == "_":
            escaped.append("__")
        elif _is_legacy_label_rune(ch, index):
            escaped.append(ch)
        else:
            escaped.append(f"_{ord(ch):x}_")
    return "".join(escaped)


@_tool(
    name=_tool_name("list_label_values"),
    description="List all values for a label, optionally restricted to series matching selectors and a time range",
    annotations={
        "title": "List Label Values",
        "icon": "🔤",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def list_label_values(
    label_name: str,
    match: Optional[List[str]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Any]:
    """List values for a specific label name.

    Args:
        label_name: The label name to retrieve values for (e.g. 'job', 'instance')
        match: Optional list of series selectors to restrict the values
        start: Optional start time as RFC3339 or Unix timestamp
        end: Optional end time as RFC3339 or Unix timestamp

    Returns:
        Dictionary containing:
        - values: List of values for the label
        - count: Number of values returned
    """
    if not label_name:
        raise ValueError("label_name must not be empty")

    logger.info("Listing label values", label_name=label_name, match=match, start=start, end=end)

    params: Dict[str, Any] = {}
    if match:
        params["match[]"] = match
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    data = make_prometheus_request(f"label/{_escape_label_name(label_name)}/values", params=params or None)

    result = {
        "values": data,
        "count": len(data)
    }

    logger.info("Label values retrieved", label_name=label_name, count=len(data))
    return result

@_tool(
    name=_tool_name("find_series"),
    description="Find time series matching label selectors, with optional time range and result limit",
    annotations={
        "title": "Find Series",
        "icon": "🔍",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def find_series(
    match: List[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Find time series by label matchers.

    Args:
        match: List of series selectors; at least one is required
        start: Optional start time as RFC3339 or Unix timestamp
        end: Optional end time as RFC3339 or Unix timestamp
        limit: Maximum number of series to return; must be positive (default: all)

    Returns:
        Dictionary containing:
        - series: List of label sets identifying matching series
        - returned_count: Number of series returned
        - has_more: Whether more series matched than were returned
    """
    if not match:
        raise ValueError("find_series requires at least one series selector in 'match'")
    if limit is not None and limit < 1:
        raise ValueError("limit must be a positive number")

    logger.info("Finding series", match=match, start=start, end=end, limit=limit)

    params: Dict[str, Any] = {"match[]": match}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    if limit is not None:
        params["limit"] = limit + 1

    data = make_prometheus_request("series", params=params)

    series = data[:limit] if limit is not None else data

    result = {
        "series": series,
        "returned_count": len(series),
        "has_more": len(series) < len(data)
    }

    logger.info("Series retrieved", returned_count=len(series), has_more=result["has_more"])
    return result

@_tool(
    name=_tool_name("get_runtime_info"),
    description="Get Prometheus runtime information such as start time, config reload status, goroutine count, and storage retention",
    annotations={
        "title": "Get Runtime Info",
        "icon": "⚙️",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def get_runtime_info() -> Dict[str, Any]:
    """Get runtime information about the Prometheus server."""
    logger.info("Retrieving runtime info")
    data = make_prometheus_request("status/runtimeinfo")

    logger.info("Runtime info retrieved")
    return data

@_tool(
    name=_tool_name("get_build_info"),
    description="Get Prometheus build information such as version, revision, and Go version",
    annotations={
        "title": "Get Build Info",
        "icon": "🏗️",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def get_build_info() -> Dict[str, Any]:
    """Get build information about the Prometheus server."""
    logger.info("Retrieving build info")
    data = make_prometheus_request("status/buildinfo")

    logger.info("Build info retrieved", version=data.get("version"))
    return data

@_tool(
    name=_tool_name("get_tsdb_stats"),
    description="Get TSDB cardinality statistics: head series counts and top metrics by series count",
    annotations={
        "title": "Get TSDB Stats",
        "icon": "💾",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def get_tsdb_stats(limit: Optional[int] = None) -> Dict[str, Any]:
    """Get TSDB usage and cardinality statistics from Prometheus.

    Args:
        limit: Maximum number of items to return per stats list; must be positive (default: 10)

    Returns:
        TSDB statistics including head block stats and cardinality breakdowns
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be a positive number")

    logger.info("Retrieving TSDB stats", limit=limit)

    params = {"limit": limit} if limit is not None else None
    data = make_prometheus_request("status/tsdb", params=params)

    logger.info("TSDB stats retrieved")
    return data

@_tool(
    name=_tool_name("query_exemplars"),
    description="Query exemplars: retrieve trace span references linked to metric samples (Prometheus 2.26+). Bridges metrics to distributed traces.",
    annotations={
        "title": "Query Exemplars",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def query_exemplars(
    query: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Any]:
    """Query exemplars from Prometheus.

    Args:
        query: PromQL selector expression (e.g. 'http_request_duration_seconds_bucket')
        start: Start time as RFC 3339 or Unix timestamp
        end: End time as RFC 3339 or Unix timestamp

    Returns:
        Exemplars data with series_count and exemplar_count
    """
    logger.info("Querying exemplars", query=query, start=start, end=end)

    params: Dict[str, Any] = {"query": query}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end

    try:
        data = make_prometheus_request("query_exemplars", params=params)
    except Exception:
        logger.warning("Exemplars query failed (Prometheus may not support exemplars)", query=query)
        data = []

    if not isinstance(data, list):
        data = []

    series_count = len(data)
    exemplar_count = sum(len(s.get("exemplars", [])) for s in data)

    logger.info("Exemplars retrieved", series_count=series_count, exemplar_count=exemplar_count)
    return {
        "exemplars": data,
        "series_count": series_count,
        "exemplar_count": exemplar_count,
    }

# ---------------------------------------------------------------------------
# MCP 2026-07-28 compatibility layer
#
# The installed mcp SDK implements 2025-11-25, so every 2026-07-28 behaviour is
# added here on top of it (see prometheus_mcp_server.spec2026). Everything below
# is additive and switched off wholesale by PROMETHEUS_MCP_SPEC_2026=false.
# ---------------------------------------------------------------------------

SERVER_VERSION = "1.6.2"

from prometheus_mcp_server.spec2026.asgi import (
    strict_header_middleware,
    tool_annotation_lookup,
)
from prometheus_mcp_server.spec2026.discovery import install_discovery
from prometheus_mcp_server.spec2026.envelope import (
    _WRAPPER_MARKER as _ENVELOPE_WRAPPER_MARKER,
    _get_request_handlers,
    install_envelope,
    install_initialize_envelope,
)
from prometheus_mcp_server.spec2026.negotiation import (
    NegotiationError,
    extract_request_meta,
    negotiation_from_meta,
    reset_current_negotiation,
    set_current_negotiation,
)
from prometheus_mcp_server.spec2026.otel import (
    extract_trace_headers,
    reset_trace_headers,
    set_trace_headers,
)

try:
    from mcp.server.lowlevel.server import request_ctx as _sdk_request_ctx
except Exception as e:  # pragma: no cover
    logger.warning(
        "MCP SDK request context unavailable; request _meta will not be read",
        error=str(e),
        error_type=type(e).__name__,
    )
    _sdk_request_ctx = None


def _current_request_meta() -> Any:
    """Read the raw _meta of the request currently being served."""
    if _sdk_request_ctx is None:
        return None
    try:
        return getattr(_sdk_request_ctx.get(), "meta", None)
    except LookupError:
        return None
    except Exception as e:
        logger.warning(
            "Could not read the SDK request context",
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


_NEGOTIATION_WRAPPER_MARKER = "_spec2026_negotiation_inner"
_SPEC_2026_WRAPPER_MARKERS = (_ENVELOPE_WRAPPER_MARKER, _NEGOTIATION_WRAPPER_MARKER)


def _unwrap_spec_2026_handler(handler: Any) -> Any:
    """Strip every 2026-07-28 wrapper off a low-level request handler."""
    while True:
        for marker in _SPEC_2026_WRAPPER_MARKERS:
            inner = getattr(handler, marker, None)
            if inner is not None:
                handler = inner
                break
        else:
            return handler


def _wrap_negotiation(handler: Any) -> Any:
    """Build the per-request negotiation wrapper around a low-level handler."""

    async def negotiation_handler(req: Any = None) -> Any:
        meta = extract_request_meta({"_meta": _current_request_meta()})

        try:
            negotiation = negotiation_from_meta(meta)
        except NegotiationError as e:
            logger.warning(
                "Rejecting request after failed protocol negotiation",
                code=e.code,
                error=e.message,
            )
            raise e.to_mcp_error() from e

        negotiation_token = set_current_negotiation(negotiation)
        trace_token = set_trace_headers(extract_trace_headers(meta))
        try:
            result = handler(req)
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            reset_current_negotiation(negotiation_token)
            reset_trace_headers(trace_token)

    setattr(negotiation_handler, _NEGOTIATION_WRAPPER_MARKER, handler)
    return negotiation_handler


def install_negotiation(server: Any = None) -> bool:
    """Wrap a FastMCP server's request handlers with per-request negotiation."""
    try:
        handlers = _get_request_handlers(server if server is not None else mcp)
        if handlers is None:
            return False

        wrapped_count = 0
        for request_type in list(handlers.keys()):
            inner = _unwrap_spec_2026_handler(handlers[request_type])
            if not callable(inner):
                logger.warning(
                    "Skipping non-callable request handler",
                    request_type=getattr(request_type, "__name__", repr(request_type)),
                )
                continue
            handlers[request_type] = _wrap_negotiation(inner)
            wrapped_count += 1

        if wrapped_count == 0:
            logger.warning("Negotiation wrapped no request handlers; layer inactive")
            return False

        logger.info("MCP 2026-07-28 negotiation installed", handler_count=wrapped_count)
        return True

    except Exception as e:
        logger.warning(
            "Per-request negotiation unavailable; server continues on 2025-11-25",
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


async def tool_header_annotations(tool_name: Any) -> List[Any]:
    """Collect the x-mcp-header annotations declared by a tool's input schema."""
    if not isinstance(tool_name, str):
        return []
    return await tool_annotation_lookup(mcp, tool_name)


def strict_header_asgi_middleware() -> Any:
    """Build the ASGI middleware enforcing the Mcp-* header rules."""
    if not config.strict_headers:
        return None
    try:
        return strict_header_middleware(annotation_lookup=tool_header_annotations)
    except Exception as e:
        logger.warning(
            "Strict header validation unavailable; requests will not be checked",
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def install_spec_2026() -> Dict[str, Any]:
    """Install the MCP 2026-07-28 compatibility layer onto this module's server."""
    status: Dict[str, Any] = {
        "negotiation": False,
        "discovery": {},
        "envelope": False,
        "initialize_envelope": False,
    }

    try:
        status["discovery"] = install_discovery(
            mcp,
            mcp_name,
            SERVER_VERSION,
            tool_namer=_tool_name,
            cache_scope=config.cache_scope,
        )

        status["negotiation"] = install_negotiation(mcp)

        status["envelope"] = install_envelope(
            mcp,
            server_name=mcp_name,
            server_version=SERVER_VERSION,
            ttl_ms=config.cache_ttl_ms,
            cache_scope=config.cache_scope,
        )

        status["initialize_envelope"] = install_initialize_envelope(
            server_name=mcp_name,
            server_version=SERVER_VERSION,
        )

        logger.info(
            "MCP 2026-07-28 compatibility layer installed",
            strict_headers=config.strict_headers,
            **status,
        )
    except Exception as e:
        logger.warning(
            "MCP 2026-07-28 compatibility layer unavailable; server continues on 2025-11-25",
            error=str(e),
            error_type=type(e).__name__,
        )

    return status


if config.spec_2026_enabled:
    SPEC_2026_STATUS = install_spec_2026()
else:
    SPEC_2026_STATUS = {}
    logger.info(
        "MCP 2026-07-28 compatibility layer disabled",
        switch="PROMETHEUS_MCP_SPEC_2026",
    )


if __name__ == "__main__":
    logger.info("Starting Prometheus MCP Server", mode="direct")
    mcp.run()
