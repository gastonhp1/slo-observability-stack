"""Runtime fault injection.

Every service keeps one ``FaultConfig``. A middleware applies it to each
business request (health, metrics and admin endpoints are never affected), so
chaos experiments and the e2e test can burn error budget on demand.
"""

from __future__ import annotations

import asyncio
import random
import threading
from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field


class FaultSpec(BaseModel):
    error_rate: float = Field(0.0, ge=0.0, le=1.0, description="Fraction of requests answered with 503")
    latency_ms: int = Field(0, ge=0, le=30_000, description="Extra latency added to affected requests")
    latency_rate: float = Field(0.0, ge=0.0, le=1.0, description="Fraction of requests that get latency_ms")


@dataclass
class FaultConfig:
    error_rate: float = 0.0
    latency_ms: int = 0
    latency_rate: float = 0.0


class FaultInjector:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._lock = threading.Lock()
        self._config = FaultConfig()
        self._rng = rng or random.Random()

    @property
    def config(self) -> FaultConfig:
        with self._lock:
            return FaultConfig(**asdict(self._config))

    def set(self, spec: FaultSpec) -> FaultConfig:
        with self._lock:
            self._config = FaultConfig(**spec.model_dump())
            return FaultConfig(**asdict(self._config))

    def clear(self) -> FaultConfig:
        with self._lock:
            self._config = FaultConfig()
            return FaultConfig()

    def should_fail(self) -> bool:
        cfg = self.config
        return cfg.error_rate > 0 and self._rng.random() < cfg.error_rate

    def delay_seconds(self) -> float:
        cfg = self.config
        if cfg.latency_ms > 0 and cfg.latency_rate > 0 and self._rng.random() < cfg.latency_rate:
            return cfg.latency_ms / 1000.0
        return 0.0

    async def apply_latency(self) -> float:
        delay = self.delay_seconds()
        if delay:
            await asyncio.sleep(delay)
        return delay
