from __future__ import annotations

import random
from contextlib import AsyncExitStack

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shop.app import create_app
from shop.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class HostRouter(httpx.AsyncBaseTransport):
    """Sends each request to the in-process ASGI app registered for its host."""

    def __init__(self, apps: dict[str, httpx.ASGITransport]) -> None:
        # The registry is shared, but each service gets its own transport object:
        # HTTPXClientInstrumentor wraps the transport, so sharing one would nest
        # one client span per service that instrumented it.
        self.apps = apps

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self.apps.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"no app for host {request.url.host}", request=request)
        return await transport.handle_async_request(request)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(spans: InMemorySpanExporter) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    return provider


@pytest.fixture
async def shop(tracer_provider):
    """The three services wired together in-process. Yields {name: (app, client)}."""
    registry: dict[str, httpx.ASGITransport] = {}
    apps = {}
    for name in ("storefront", "orders", "payments"):
        settings = Settings(
            service=name,
            orders_url="http://orders",
            payments_url="http://payments",
            otlp_endpoint="",
            admin_token="",
        )
        apps[name] = create_app(
            settings,
            tracer_provider=tracer_provider,
            transport=HostRouter(registry),
            rng=random.Random(42),
            configure_logging=False,
        )
        registry[name] = httpx.ASGITransport(app=apps[name])

    async with AsyncExitStack() as stack:
        result = {}
        for name, app in apps.items():
            await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{name}")
            )
            result[name] = (app, client)
        yield result
