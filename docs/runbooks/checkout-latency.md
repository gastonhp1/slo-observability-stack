# Runbook: CheckoutLatency

| | |
|---|---|
| **Alert** | `CheckoutLatency` (page and ticket) |
| **SLO** | `storefront-checkout-latency`: 99% of checkouts served in under 500ms over 30d |
| **Dashboards** | [Service RED](http://localhost:3000/d/service-red?var-service=storefront), [SLO overview](http://localhost:3000/d/slo-overview) |
| **Owner** | team `shop` |

## What it means

Too many checkouts take longer than 500ms. The SLI counts requests above the
`le="0.5"` histogram bucket, so it is exact at that threshold (no percentile
estimation involved).

## Triage

1. **Which hop is slow?** Compare p99 per service:
   ```promql
   histogram_quantile(0.99, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m])))
   ```
   Latency is cumulative: if payments is slow, orders and storefront are too.
   The deepest slow service is the one to look at.
2. **Open a slow trace.** On *Service RED*, the latency panel shows exemplars on
   p99; click one above the 500ms line. The longest span is where the time went.
   ```traceql
   {resource.service.name="storefront" && span.http.route="/checkout" && duration > 500ms}
   ```
3. **Injected?** A span event `fault.latency` on the payments span means a chaos
   experiment is adding delay. `scripts/chaos.sh status payments`.
4. **Saturation?** `http_requests_in_flight` climbing means requests queue up;
   check traffic (`Requests / s`) against what the service normally handles.

## Mitigate

| Cause | Action |
|---|---|
| Fault injection | `scripts/chaos.sh clear all` |
| Slow dependency | Escalate to its owner. If the slowness is above the 2s client timeout, checkout turns into 504s and `CheckoutAvailability` will page too. |
| Load spike | Scale out storefront/orders/payments; the services are stateless (orders store is per-instance and demo-only). |
| Bad deploy | Roll back. |

## Note on the threshold

The 500ms threshold must exist as a histogram bucket (`LATENCY_BUCKETS` in
`services/shop/metrics.py`). Changing the SLO threshold without adding the
bucket makes the SLI return no data, which `SLIMetricsMissing` will not catch
(the counter still exists). Change both in the same PR.
