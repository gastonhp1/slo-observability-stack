"""RED metrics (rate, errors, duration) exposed for Prometheus.

The SLIs in ``slo/specs`` are built on these two series, so their names and
labels are a contract: changing them means regenerating the SLO rules.

* ``http_requests_total{service, route, method, code}``
* ``http_request_duration_seconds_bucket{service, route, method, le}``

``route`` is the route *template* (``/products/{product_id}``), never the raw
path, to keep cardinality bounded. Duration observations carry the current
trace id as an OpenMetrics exemplar so Grafana can jump from a latency spike
to the exact trace.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import choose_encoder

# Bucket bounds must include every latency threshold used by an SLO spec
# (0.3s and 0.5s today), otherwise the "good events" query has no series.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0)


class ShopMetrics:
    def __init__(self, service: str, version: str, registry: CollectorRegistry | None = None) -> None:
        self.service = service
        self.registry = registry or CollectorRegistry()
        self.requests = Counter(
            "http_requests",
            "HTTP requests handled, by route template and status code.",
            ["service", "route", "method", "code"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds, by route template.",
            ["service", "route", "method"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "http_requests_in_flight",
            "HTTP requests currently being served.",
            ["service"],
            registry=self.registry,
        )
        self.build_info = Gauge(
            "shop_build_info",
            "Build metadata; always 1.",
            ["service", "version"],
            registry=self.registry,
        )
        self.build_info.labels(service=service, version=version).set(1)
        self.faults_injected = Counter(
            "shop_faults_injected",
            "Requests affected by fault injection, by kind (error|latency).",
            ["service", "kind"],
            registry=self.registry,
        )

    def observe(self, route: str, method: str, code: int, seconds: float, trace_id: str | None) -> None:
        self.requests.labels(self.service, route, method, str(code)).inc()
        exemplar = {"trace_id": trace_id} if trace_id else None
        self.duration.labels(self.service, route, method).observe(seconds, exemplar=exemplar)

    def render(self, accept_header: str | None) -> tuple[bytes, str]:
        """Return (body, content type). OpenMetrics is used when the scraper asks for it,
        which is what makes exemplars visible to Prometheus."""
        encoder, content_type = choose_encoder(accept_header or "")
        return encoder(self.registry), content_type
