# Runbook: ShopTargetDown

| | |
|---|---|
| **Alert** | `ShopTargetDown` (page) |
| **Condition** | `up{job="shop"} == 0` for 2 minutes |
| **Owner** | team `shop` |

## What it means

Prometheus cannot scrape one of storefront, orders or payments. Either the
process is down or it is unreachable from Prometheus.

This alert exists because the SLO alerts are blind to it: a dead service
produces **no** request metrics, so its error ratio is "no data", not 100%. If
storefront itself is down, no SLO alert will ever fire. While this alert is
active, Alertmanager inhibits the availability SLO alerts, since they describe
the same incident.

## Triage

```bash
docker compose ps
docker compose logs --tail=100 <service>
curl -s localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.health != "up") | {scrapeUrl, lastError}'
```

* `connection refused` → process not listening (crashed, restarting, wrong port).
* `context deadline exceeded` → process alive but stuck, or the node is overloaded.
* Healthcheck failing in `docker compose ps` → the app is not answering `/healthz`.

## Mitigate

1. Restart: `docker compose restart <service>`.
2. If it crash-loops, check the last logs for the exception and roll back the
   last change.
3. Once `up` is 1 again, the SLO alerts become visible again: check *SLO
   overview* for the damage done to the budget during the outage. Note: while
   the service was down, the SLI had no data, so the outage itself **is not
   counted** in the budget. Record its duration in the postmortem by hand.
