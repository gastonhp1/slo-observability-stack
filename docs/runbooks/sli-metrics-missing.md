# Runbook: SLIMetricsMissing

| | |
|---|---|
| **Alert** | `SLIMetricsMissing` (ticket) |
| **Condition** | no `http_requests_total{service="storefront", route="/checkout"}` series for 10 minutes |
| **Owner** | team `shop` |

## What it means

The checkout SLOs are computed from that series. Without it they silently show
"no data" and **cannot alert**. Causes, from most to least likely:

1. The route or a label changed in code (`/checkout` → `/v2/checkout`, or the
   `service` label value) and the SLO specs were not updated.
2. The metric was renamed or its type changed.
3. There is genuinely no checkout traffic (loadgen stopped, or real traffic is zero).
4. Storefront is not being scraped (then `ShopTargetDown` fires as well).

## What to do

```promql
count by (route) (http_requests_total{service="storefront"})
```

* A new route name appears → update `slo/specs/storefront.yml`, run `make slo`,
  commit the generated rules.
* Nothing for storefront at all → see [ShopTargetDown](shop-target-down.md).
* Only checkout is missing and that is expected (no traffic) → silence the
  alert for the expected period in Alertmanager, with a comment.

Prevention: the service tests assert the exact metric names and labels the
SLIs depend on (`services/tests/test_services.py`), so a rename should fail CI
before it reaches this alert.
