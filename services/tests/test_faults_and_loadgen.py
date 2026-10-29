from __future__ import annotations

import random
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from shop import alertsink
from shop.faults import FaultInjector, FaultSpec
from shop.loadgen import pick_request


def test_fault_rates_are_respected():
    injector = FaultInjector(random.Random(7))
    injector.set(FaultSpec(error_rate=0.25))
    failures = sum(injector.should_fail() for _ in range(10_000))
    assert 2_300 < failures < 2_700


def test_clear_disables_faults():
    injector = FaultInjector(random.Random(7))
    injector.set(FaultSpec(error_rate=1.0, latency_ms=100, latency_rate=1.0))
    injector.clear()
    assert not any(injector.should_fail() for _ in range(100))
    assert injector.delay_seconds() == 0


@pytest.mark.parametrize("bad", [{"error_rate": 1.5}, {"latency_rate": -0.1}, {"latency_ms": 60_000}])
def test_fault_spec_validation(bad):
    with pytest.raises(ValueError):
        FaultSpec(**bad)


def test_loadgen_mix_hits_every_route():
    rng = random.Random(1)
    paths = Counter()
    for _ in range(5_000):
        method, path, _ = pick_request(rng)
        paths["detail" if path.startswith("/products/") else path] += 1
    assert paths["/products"] > paths["detail"] > paths["/checkout"] > 0


def test_alertsink_records_alertmanager_webhook():
    alertsink.received.clear()
    payload = {
        "receiver": "pager",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "CheckoutAvailabilityBudgetBurn", "severity": "page", "slo": "x"},
                "annotations": {"summary": "burning"},
                "startsAt": "2026-10-29T00:00:00Z",
            }
        ],
    }
    client = TestClient(alertsink.app)
    assert client.post("/alerts/pager", json=payload).json() == {"accepted": 1}
    got = client.get("/alerts", params={"alertname": "CheckoutAvailabilityBudgetBurn"}).json()
    assert got[0]["severity"] == "page"
    assert "alertsink_notifications_total" in client.get("/metrics").text
