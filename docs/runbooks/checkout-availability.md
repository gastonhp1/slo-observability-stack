# Runbook: CheckoutAvailability

| | |
|---|---|
| **Alert** | `CheckoutAvailability` (page and ticket) |
| **SLO** | `storefront-checkout-availability`: 99.9% of checkouts without 5xx over 30d |
| **Dashboards** | [SLO overview](http://localhost:3000/d/slo-overview), [Service RED](http://localhost:3000/d/service-red?var-service=storefront) |
| **Owner** | team `shop` |

## What it means

Checkouts are failing with server errors fast enough to spend a meaningful part
of the 30-day error budget:

* **page**: at least 2% of the budget in the last hour (14.4x) or 5% in the last 6h (6x).
* **ticket**: at least 10% over 1 or 3 days. Slow, but it will breach the SLO if nobody acts.

Users are seeing "something went wrong" when they try to pay. This is revenue.

## Triage (first 5 minutes)

1. **How bad?** On *SLO overview*, check the 5m burn rate. 14.4x means the
   whole month's budget goes in ~2 days; 500x means it is going in ~1.5 hours.
2. **Where?** Checkout calls `orders`, which calls `payments`. On *Service RED*
   switch the service variable: the deepest service with 5xx is usually the cause.
   A 502 at storefront means a dependency failed; 504 means it timed out.
   ```promql
   sum by (service, code) (rate(http_requests_total{code=~"5.."}[5m]))
   ```
3. **Is it injected?** If *Faults injected / s* is non-zero, someone is running a
   chaos experiment. Check `scripts/chaos.sh status payments` and confirm with them.
4. **What changed?** A deploy in the last hour (`shop_build_info` version label),
   a config change, a dependency incident.

## Find the cause

* **From the metric to a trace:** on *Service RED*, latency panel, click a dot
  (exemplar) to open the trace. Failed spans are red; the deepest red span is
  where it broke.
* **Search failed traces directly** (Grafana → Explore → Tempo):
  ```traceql
  {resource.service.name="storefront" && span.http.route="/checkout" && status=error}
  ```
* **From the trace to logs:** in the trace view, *Logs for this span* opens Loki
  filtered by the trace id. Or in Explore → Loki:
  ```logql
  {service_name=~"storefront|orders|payments"} | severity_text=~"(?i)error"
  ```

## Mitigate

Restore service first, find the root cause later.

| Cause | Action |
|---|---|
| Fault injection left on | `scripts/chaos.sh clear all` |
| Bad deploy | Roll back to the previous image. Do not debug forward during a page. |
| `payments` down or erroring | Check its own dashboard and the `PaymentsCaptureAvailability` ticket. Escalate to team `payments`. |
| Dependency timeouts (504) | Check latency of the callee; scale it or shed load. `DOWNSTREAM_TIMEOUT_SECONDS` defaults to 2s. |
| Process not running | That is `ShopTargetDown`; see [its runbook](shop-target-down.md). |

## Afterwards

* The alert resolves on its own once the short window (5m or 30m) drops below
  the threshold, a few minutes after the fix.
* If the budget dropped below 50%, apply the [error budget policy](../slo-policy.md#error-budget-policy).
* Pages need a postmortem when budget left goes below 0 or the impact lasted
  more than 30 minutes. Template: [postmortem example](../postmortems/2026-10-29-payments-5xx.md).
