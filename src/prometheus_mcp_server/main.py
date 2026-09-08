#!/usr/bin/env python
import logging
import signal
import sys
import threading
import dotenv
from prometheus_mcp_server.server import (
    mcp,
    config,
    strict_header_asgi_middleware,
    TransportType,
)
from prometheus_mcp_server.logging_config import setup_logging

# Initialize structured logging
logger = setup_logging()


class _ShutdownCancelledFilter(logging.Filter):
    """Suppress noisy CancelledError tracebacks from uvicorn during shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[1] is not None:
            import asyncio
            if isinstance(record.exc_info[1], asyncio.CancelledError):
                return False
        return True

# Global shutdown event for graceful Docker shutdown
shutdown_event = threading.Event()

def signal_handler(signum, frame):
    """Handle SIGTERM and SIGINT signals for graceful shutdown."""
    signal_name = signal.Signals(signum).name
    logger.info("Received shutdown signal", signal=signal_name)
    shutdown_event.set()

def setup_environment():
    if dotenv.load_dotenv():
        logger.info("Environment configuration loaded", source=".env file")
    else:
        logger.info("Environment configuration loaded", source="environment variables", note="No .env file found")

    if not config.url:
        logger.error(
            "Missing required configuration",
            error="PROMETHEUS_URL environment variable is not set",
            suggestion="Please set it to your Prometheus server URL",
            example="http://your-prometheus-server:9090"
        )
        return False
    
    # MCP Server configuration validation
    mcp_config = config.mcp_server_config
    if mcp_config:
        if str(mcp_config.mcp_server_transport).lower() not in TransportType.values():
            logger.error(
                "Invalid mcp transport",
                error="PROMETHEUS_MCP_SERVER_TRANSPORT environment variable is invalid",
                suggestion="Please define one of these acceptable transports (http/sse/stdio)",
                example="http"
            )
            return False

        try:
            if mcp_config.mcp_bind_port:
                int(mcp_config.mcp_bind_port)
        except (TypeError, ValueError):
            logger.error(
                "Invalid mcp port",
                error="PROMETHEUS_MCP_BIND_PORT environment variable is invalid",
                suggestion="Please define an integer",
                example="8080"
            )
            return False
    
    # Determine authentication method
    auth_method = "none"
    if config.username and config.password:
        auth_method = "basic_auth"
    elif config.token:
        auth_method = "bearer_token"
    
    logger.info(
        "Prometheus configuration validated",
        server_url=config.url,
        authentication=auth_method,
        org_id=config.org_id if config.org_id else None
    )
    
    return True

def run_server():
    """Main entry point for the Prometheus MCP Server"""
    # Setup signal handlers for graceful Docker shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Setup environment
    if not setup_environment():
        logger.error("Environment setup failed, exiting")
        sys.exit(1)

    mcp_config = config.mcp_server_config
    transport = mcp_config.mcp_server_transport

    http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
    if transport in http_transports:
        # Suppress noisy CancelledError tracebacks during graceful shutdown
        logging.getLogger("uvicorn.error").addFilter(_ShutdownCancelledFilter())

        asgi_middleware = strict_header_asgi_middleware()
        logger.info("Starting Prometheus MCP Server",
                transport=transport,
                host=mcp_config.mcp_bind_host,
                port=mcp_config.mcp_bind_port,
                stateless_http=mcp_config.stateless_http)
        mcp.run(
            transport=transport,
            host=mcp_config.mcp_bind_host,
            port=mcp_config.mcp_bind_port,
            uvicorn_config={"timeout_graceful_shutdown": 5},
            **({"middleware": [asgi_middleware]} if asgi_middleware else {}),
            **({"stateless_http": True} if mcp_config.stateless_http else {})
        )
    else:
        logger.info("Starting Prometheus MCP Server", transport=transport)
        # Run stdio transport in a thread so signal handlers can trigger graceful shutdown
        server_thread = threading.Thread(target=lambda: mcp.run(transport=transport))
        server_thread.daemon = True
        server_thread.start()
        try:
            shutdown_event.wait()
            logger.info("Shutdown initiated, stopping server gracefully")
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received, stopping server")
        finally:
            logger.info("Server shutdown complete")
            sys.exit(0)

if __name__ == "__main__":
    run_server()
