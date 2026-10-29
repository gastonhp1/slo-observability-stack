"""Steady synthetic traffic so dashboards and SLO rules always have data.

Mix (per request): 60% list products, 25% product detail (some unknown ids ->
404), 15% checkout (a few oversized orders -> 402 declined). 4xx responses are
expected client errors and do not burn availability budget.

    python -m shop.loadgen            # uses LOADGEN_* env vars
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import Counter

import httpx

from .app import CATALOG

log = logging.getLogger("shop.loadgen")


def pick_request(rng: random.Random) -> tuple[str, str, dict | None]:
    roll = rng.random()
    if roll < 0.60:
        return "GET", "/products", None
    if roll < 0.85:
        product = rng.choice(list(CATALOG)) if rng.random() > 0.05 else "p-999"
        return "GET", f"/products/{product}", None
    quantity = rng.randint(1, 3) if rng.random() > 0.02 else 500
    return "POST", "/checkout", {"product_id": rng.choice(list(CATALOG)), "quantity": quantity}


async def run(base_url: str, rps: float, duration_s: float | None, seed: int | None = None) -> Counter:
    rng = random.Random(seed)
    stats: Counter = Counter()
    interval = 1.0 / rps
    deadline = time.monotonic() + duration_s if duration_s else None
    last_report = time.monotonic()
    in_flight: set[asyncio.Task] = set()

    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:

        async def one() -> None:
            method, path, body = pick_request(rng)
            try:
                resp = await client.request(method, path, json=body)
                stats[f"{resp.status_code // 100}xx"] += 1
            except httpx.HTTPError:
                stats["conn_error"] += 1

        while deadline is None or time.monotonic() < deadline:
            task = asyncio.create_task(one())
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
            await asyncio.sleep(interval)
            if time.monotonic() - last_report >= 30:
                log.info("loadgen stats %s", dict(stats))
                last_report = time.monotonic()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    base_url = os.getenv("LOADGEN_TARGET", "http://storefront:8080")
    rps = float(os.getenv("LOADGEN_RPS", "10"))
    duration = os.getenv("LOADGEN_DURATION_SECONDS")
    log.info("starting loadgen target=%s rps=%s", base_url, rps)
    stats = asyncio.run(run(base_url, rps, float(duration) if duration else None))
    log.info("loadgen finished %s", dict(stats))


if __name__ == "__main__":
    main()
