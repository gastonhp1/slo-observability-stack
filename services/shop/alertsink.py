"""Stand-in for PagerDuty / Jira: receives Alertmanager webhooks.

Every notification is logged as JSON (so it lands in Loki next to the service
logs) and kept in a small in-memory ring buffer that the e2e test and humans
can inspect at ``GET /alerts``.

    uvicorn shop.alertsink:app --port 8080
"""

from __future__ import annotations

import json
import logging
import sys
from collections import deque
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from prometheus_client import CollectorRegistry, Counter
from prometheus_client.exposition import choose_encoder

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("alertsink")

registry = CollectorRegistry()
notifications = Counter(
    "alertsink_notifications",
    "Alert notifications received, by receiver, alert name, status and severity.",
    ["receiver", "alertname", "status", "severity"],
    registry=registry,
)

received: deque[dict] = deque(maxlen=200)
app = FastAPI(title="alert-sink")


@app.post("/alerts/{receiver}")
async def receive(receiver: str, request: Request) -> dict:
    payload = await request.json()
    now = datetime.now(UTC).isoformat()
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        entry = {
            "received_at": now,
            "receiver": receiver,
            "status": alert.get("status"),
            "alertname": labels.get("alertname"),
            "severity": labels.get("severity"),
            "slo_id": labels.get("slo_id"),
            "labels": labels,
            "annotations": alert.get("annotations", {}),
            "startsAt": alert.get("startsAt"),
        }
        received.append(entry)
        notifications.labels(receiver, entry["alertname"] or "", entry["status"] or "", entry["severity"] or "").inc()
        log.info(json.dumps({"msg": "alert notification", **entry}))
    return {"accepted": len(payload.get("alerts", []))}


@app.get("/alerts")
async def list_alerts(receiver: str | None = None, alertname: str | None = None) -> list[dict]:
    return [
        a
        for a in received
        if (receiver is None or a["receiver"] == receiver) and (alertname is None or a["alertname"] == alertname)
    ]


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    encoder, content_type = choose_encoder(request.headers.get("accept", ""))
    return Response(content=encoder(registry), media_type=content_type)
