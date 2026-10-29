"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    service: str = field(default_factory=lambda: _env("SHOP_SERVICE", "storefront"))
    version: str = field(default_factory=lambda: _env("SHOP_VERSION", "0.1.0"))
    environment: str = field(default_factory=lambda: _env("SHOP_ENVIRONMENT", "local"))
    orders_url: str = field(default_factory=lambda: _env("ORDERS_URL", "http://orders:8080"))
    payments_url: str = field(default_factory=lambda: _env("PAYMENTS_URL", "http://payments:8080"))
    # OTLP is optional: with no endpoint the services still run and expose /metrics.
    otlp_endpoint: str = field(default_factory=lambda: _env("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
    downstream_timeout_s: float = field(default_factory=lambda: float(_env("DOWNSTREAM_TIMEOUT_SECONDS", "2.0")))
    # Empty token disables auth on /admin/faults (fine for local demos only).
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", ""))


def load() -> Settings:
    return Settings()
