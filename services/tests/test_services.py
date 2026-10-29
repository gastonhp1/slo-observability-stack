from __future__ import annotations

import pytest

from shop.app import create_app
from shop.config import Settings

pytestmark = pytest.mark.anyio


def sample(app, name: str, **labels) -> float:
    value = app.state.metrics.registry.get_sample_value(name, labels)
    return value or 0.0


async def test_products(shop):
    _, client = shop["storefront"]
    resp = await client.get("/products")
    assert resp.status_code == 200
    assert {p["id"] for p in resp.json()} >= {"p-100", "p-200"}


async def test_unknown_product_is_404_and_labelled_with_route_template(shop):
    app, client = shop["storefront"]
    assert (await client.get("/products/p-999")).status_code == 404
    # The raw id must never become a label value (cardinality).
    assert (
        sample(
            app, "http_requests_total", service="storefront", route="/products/{product_id}", method="GET", code="404"
        )
        == 1
    )


async def test_checkout_flows_through_all_services(shop):
    _, client = shop["storefront"]
    resp = await client.post("/checkout", json={"product_id": "p-100", "quantity": 2})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["amount"] == 258.0
    assert body["payment"]["status"] == "captured"

    for name, route in (("storefront", "/checkout"), ("orders", "/orders"), ("payments", "/payments")):
        app, _ = shop[name]
        assert sample(app, "http_requests_total", service=name, route=route, method="POST", code="201") == 1


async def test_trace_context_propagates_across_services(shop, spans):
    _, client = shop["storefront"]
    await client.post("/checkout", json={"product_id": "p-200", "quantity": 1})
    finished = spans.get_finished_spans()
    # In-process the downstream spans are INTERNAL (the context is shared); over
    # the network they are SERVER spans. What matters is one trace end to end.
    handlers = {
        s.name: s
        for s in finished
        if s.instrumentation_scope.name.endswith("fastapi") and not s.name.endswith(("http send", "http receive"))
    }
    assert {"POST /checkout", "POST /orders", "POST /payments"} <= set(handlers)
    assert len({s.context.trace_id for s in handlers.values()}) == 1
    client_spans = [s for s in finished if s.kind.name == "CLIENT"]
    assert len(client_spans) == 2, "one client span per downstream hop"


async def test_declined_payment_is_a_4xx_not_a_5xx(shop):
    _, client = shop["storefront"]
    resp = await client.post("/checkout", json={"product_id": "p-400", "quantity": 500})
    assert resp.status_code == 402
    app, _ = shop["storefront"]
    assert sample(app, "http_requests_total", service="storefront", route="/checkout", method="POST", code="402") == 1


async def test_payments_fault_surfaces_as_502_at_the_edge(shop):
    payments_app, payments_client = shop["payments"]
    _, storefront = shop["storefront"]
    storefront_app, _ = shop["storefront"]

    resp = await payments_client.put("/admin/faults", json={"error_rate": 1.0})
    assert resp.status_code == 200

    resp = await storefront.post("/checkout", json={"product_id": "p-100", "quantity": 1})
    assert resp.status_code == 502
    # The injected error is attributed to the real route, so it counts against the SLI.
    assert (
        sample(payments_app, "http_requests_total", service="payments", route="/payments", method="POST", code="503")
        == 1
    )
    assert sample(payments_app, "shop_faults_injected_total", service="payments", kind="error") == 1
    assert (
        sample(
            storefront_app, "http_requests_total", service="storefront", route="/checkout", method="POST", code="502"
        )
        == 1
    )

    await payments_client.delete("/admin/faults")
    resp = await storefront.post("/checkout", json={"product_id": "p-100", "quantity": 1})
    assert resp.status_code == 201


async def test_latency_fault_is_recorded_in_histogram(shop):
    app, client = shop["payments"]
    await client.put("/admin/faults", json={"latency_ms": 350, "latency_rate": 1.0})
    resp = await client.post("/payments", json={"order_id": "o-1", "amount": 10})
    assert resp.status_code == 201
    labels = dict(service="payments", route="/payments", method="POST")
    assert sample(app, "http_request_duration_seconds_bucket", le="0.3", **labels) == 0
    assert sample(app, "http_request_duration_seconds_bucket", le="0.5", **labels) == 1


async def test_operational_endpoints_do_not_count_toward_slis(shop):
    app, client = shop["orders"]
    for path in ("/healthz", "/readyz", "/metrics", "/admin/faults"):
        assert (await client.get(path)).status_code == 200
    assert (
        app.state.metrics.registry.get_sample_value(
            "http_requests_total", {"service": "orders", "route": "/healthz", "method": "GET", "code": "200"}
        )
        is None
    )


async def test_metrics_expose_exemplars_in_openmetrics(shop):
    _, client = shop["storefront"]
    await client.get("/products")
    resp = await client.get("/metrics", headers={"Accept": "application/openmetrics-text; version=1.0.0"})
    assert resp.headers["content-type"].startswith("application/openmetrics-text")
    assert "# {trace_id=" in resp.text


async def test_admin_token_is_enforced(tracer_provider):
    settings = Settings(service="payments", admin_token="s3cret", otlp_endpoint="")
    app = create_app(settings, tracer_provider=tracer_provider, configure_logging=False)
    import httpx

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://payments") as client,
    ):
        assert (await client.get("/admin/faults")).status_code == 401
        assert (await client.get("/admin/faults", headers={"X-Admin-Token": "s3cret"})).status_code == 200


def test_unknown_service_is_rejected():
    with pytest.raises(ValueError):
        create_app(Settings(service="inventory"), configure_logging=False)
