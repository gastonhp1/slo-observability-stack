"""Application factory for the three demo services.

    storefront  GET  /products, GET /products/{product_id}, POST /checkout
        └─► orders    POST /orders, GET /orders/{order_id}
               └─► payments  POST /payments

One image, three roles: ``SHOP_SERVICE`` picks which router is mounted. Every
service also exposes /metrics, /healthz, /readyz and /admin/faults.
"""

from __future__ import annotations

import itertools
import logging
import random
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel, Field

from . import telemetry
from .config import Settings
from .faults import FaultInjector, FaultSpec
from .metrics import ShopMetrics
from .middleware import ObservabilityMiddleware

SERVICES = ("storefront", "orders", "payments")

CATALOG = {
    "p-100": {"id": "p-100", "name": "Hydraulic jack 3t", "price": 129.0},
    "p-200": {"id": "p-200", "name": "Torque wrench", "price": 89.5},
    "p-300": {"id": "p-300", "name": "Ratchet strap set", "price": 24.9},
    "p-400": {"id": "p-400", "name": "Chain hoist 1t", "price": 310.0},
}

# Payments above this amount are declined with 402: a 4xx that must NOT burn
# availability budget. It is there to show that the SLIs only count 5xx.
DECLINE_ABOVE = 10_000.0


class CheckoutRequest(BaseModel):
    product_id: str
    quantity: int = Field(1, ge=1, le=1000)


class OrderRequest(BaseModel):
    product_id: str
    quantity: int = Field(ge=1)
    amount: float = Field(gt=0)


class PaymentRequest(BaseModel):
    order_id: str
    amount: float = Field(gt=0)


def _downstream_error(exc: Exception, dependency: str) -> HTTPException:
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(504, f"{dependency} timed out")
    if isinstance(exc, httpx.HTTPStatusError):
        upstream = exc.response.status_code
        if 400 <= upstream < 500:
            # Propagate client errors as-is: they are the caller's problem, not ours.
            detail = exc.response.json().get("detail", "rejected") if exc.response.content else "rejected"
            return HTTPException(upstream, detail)
        return HTTPException(502, f"{dependency} returned {upstream}")
    return HTTPException(502, f"{dependency} unreachable")


def _storefront_router(state: dict) -> APIRouter:
    router = APIRouter()
    log = logging.getLogger("shop.storefront")

    @router.get("/products")
    async def list_products() -> list[dict]:
        return list(CATALOG.values())

    @router.get("/products/{product_id}")
    async def get_product(product_id: str) -> dict:
        product = CATALOG.get(product_id)
        if not product:
            raise HTTPException(404, "product not found")
        return product

    @router.post("/checkout", status_code=201)
    async def checkout(body: CheckoutRequest) -> dict:
        product = CATALOG.get(body.product_id)
        if not product:
            raise HTTPException(404, "product not found")
        amount = round(product["price"] * body.quantity, 2)
        trace.get_current_span().set_attributes({"shop.product_id": body.product_id, "shop.amount": amount})
        try:
            resp = await state["client"].post(
                f"{state['settings'].orders_url}/orders",
                json={"product_id": body.product_id, "quantity": body.quantity, "amount": amount},
            )
            resp.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("checkout failed", extra={"fields": {"reason": str(exc)}})
            raise _downstream_error(exc, "orders") from exc
        return resp.json()

    return router


def _orders_router(state: dict) -> APIRouter:
    router = APIRouter()
    log = logging.getLogger("shop.orders")
    orders: dict[str, dict] = {}
    seq = itertools.count(1)

    @router.post("/orders", status_code=201)
    async def create_order(body: OrderRequest) -> dict:
        order_id = f"o-{next(seq):06d}"
        trace.get_current_span().set_attribute("shop.order_id", order_id)
        try:
            resp = await state["client"].post(
                f"{state['settings'].payments_url}/payments",
                json={"order_id": order_id, "amount": body.amount},
            )
            resp.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("payment failed", extra={"fields": {"order_id": order_id, "reason": str(exc)}})
            raise _downstream_error(exc, "payments") from exc
        order = {"order_id": order_id, **body.model_dump(), "payment": resp.json(), "status": "confirmed"}
        orders[order_id] = order
        if len(orders) > 10_000:  # bounded memory for long demo runs
            orders.pop(next(iter(orders)))
        return order

    @router.get("/orders/{order_id}")
    async def get_order(order_id: str) -> dict:
        order = orders.get(order_id)
        if not order:
            raise HTTPException(404, "order not found")
        return order

    return router


def _payments_router(state: dict) -> APIRouter:
    router = APIRouter()

    @router.post("/payments", status_code=201)
    async def capture(body: PaymentRequest) -> dict:
        if body.amount > DECLINE_ABOVE:
            raise HTTPException(402, "payment declined: amount over limit")
        return {"payment_id": f"pay-{uuid.uuid4().hex[:12]}", "order_id": body.order_id, "status": "captured"}

    return router


ROUTERS = {"storefront": _storefront_router, "orders": _orders_router, "payments": _payments_router}


def create_app(
    settings: Settings,
    *,
    tracer_provider: TracerProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    rng: random.Random | None = None,
    configure_logging: bool = True,
) -> FastAPI:
    if settings.service not in SERVICES:
        raise ValueError(f"unknown service {settings.service!r}, expected one of {SERVICES}")

    resource = telemetry.build_resource(settings)
    if configure_logging:
        logger = telemetry.setup_logging(settings, resource)
    else:
        logger = logging.getLogger(telemetry.LOGGER_NAME)
    if tracer_provider is None:
        tracer_provider = telemetry.setup_tracing(settings, resource)

    metrics = ShopMetrics(settings.service, settings.version)
    faults = FaultInjector(rng)
    state: dict = {"settings": settings, "client": None}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        client = httpx.AsyncClient(timeout=settings.downstream_timeout_s, transport=transport)
        HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tracer_provider)
        state["client"] = client
        logger.info("service started", extra={"fields": {"service": settings.service}})
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title=f"shop-{settings.service}", version=settings.version, lifespan=lifespan)
    app.state.metrics = metrics
    app.state.faults = faults

    async def fault_gate() -> None:
        """Applied to business routes only, after routing (see middleware.py)."""
        delay = await faults.apply_latency()
        span = trace.get_current_span()
        if delay:
            metrics.faults_injected.labels(settings.service, "latency").inc()
            span.add_event("fault.latency", {"delay_ms": int(delay * 1000)})
        if faults.should_fail():
            metrics.faults_injected.labels(settings.service, "error").inc()
            span.add_event("fault.error")
            raise HTTPException(503, "injected fault")

    app.include_router(ROUTERS[settings.service](state), dependencies=[Depends(fault_gate)])

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if settings.admin_token and x_admin_token != settings.admin_token:
            raise HTTPException(401, "invalid admin token")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict:
        if state["client"] is None:
            raise HTTPException(503, "starting")
        return {"status": "ready", "service": settings.service}

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Response:
        body, content_type = metrics.render(request.headers.get("accept"))
        return Response(content=body, media_type=content_type)

    @app.get("/admin/faults", dependencies=[Depends(require_admin)])
    async def get_faults() -> dict:
        return asdict(faults.config)

    @app.put("/admin/faults", dependencies=[Depends(require_admin)])
    async def set_faults(spec: FaultSpec) -> dict:
        cfg = faults.set(spec)
        logger.warning("fault injection updated", extra={"fields": asdict(cfg)})
        return asdict(cfg)

    @app.delete("/admin/faults", dependencies=[Depends(require_admin)])
    async def clear_faults() -> dict:
        logger.warning("fault injection cleared")
        return asdict(faults.clear())

    # Order matters: the OTel middleware must be the outer one so the span is
    # active while ObservabilityMiddleware records exemplars and logs.
    app.add_middleware(ObservabilityMiddleware, metrics=metrics, logger=logger)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls="healthz,readyz,metrics",
    )
    return app
