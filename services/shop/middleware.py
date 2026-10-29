"""ASGI middleware that records RED metrics and access logs.

It runs *inside* the OpenTelemetry ASGI middleware, so the request span is
already active: access logs and exemplars carry the trace id.

Fault injection is NOT done here but in a router dependency (see ``app.py``):
it has to run after routing so injected errors are attributed to the right
route template and therefore count against the right SLI.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .metrics import ShopMetrics
from .telemetry import current_trace_id

# Endpoints that are part of operating the service, not of serving users.
# They are excluded from metrics so they never count toward an SLI.
OPERATIONAL_PREFIXES = ("/metrics", "/healthz", "/readyz", "/admin")


def route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if path else "unmatched"


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, metrics: ShopMetrics, logger: logging.Logger) -> None:
        self.app = app
        self.metrics = metrics
        self.logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(OPERATIONAL_PREFIXES):
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status = {"code": 500}  # stays 500 if the app raises before responding
        self.metrics.in_flight.labels(self.metrics.service).inc()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            self.metrics.in_flight.labels(self.metrics.service).dec()
            # The router has filled scope["route"] by now (or not, for 404s).
            route = route_template(scope)
            code = status["code"]
            self.metrics.observe(route, scope["method"], code, elapsed, current_trace_id())
            level = logging.ERROR if code >= 500 else logging.WARNING if code >= 400 else logging.INFO
            self.logger.log(
                level,
                "request",
                extra={
                    "fields": {
                        "http.method": scope["method"],
                        "http.route": route,
                        "http.status_code": code,
                        "duration_ms": round(elapsed * 1000, 2),
                    }
                },
            )
