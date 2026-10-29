# Postmortem: payments 5xx burned the checkout error budget

> **Game day, not a real incident.** This failure was injected on purpose with
> `scripts/chaos.sh errors payments 0.6` (the same scenario CI runs in
> `tests/e2e/run.sh`). It is written up in full because the postmortem is part
> of the exercise: it tests whether the alerts, dashboards and runbooks are
> enough for someone who did not build them.

| | |
|---|---|
| **Date** | 2026-10-29 |
| **Severity** | SEV-2 (checkout degraded, no data loss) |
| **Duration of impact** | 10 minutes (fault on → fault cleared) |
| **Detection** | `CheckoutAvailability` page, 31 seconds after the fault started (measured by the CI e2e run) |
| **SLO impact** | `storefront-checkout-availability`: ~60% of checkouts failed for 10 minutes |
| **Status** | Closed. Action items below. |

## Summary

Payments started answering 60% of capture requests with 503. Orders turned
each failure into a 502, and storefront returned 502 to the user. About 6 in 10
checkouts failed for 10 minutes. The checkout availability SLO paged the
shop on-call through the fast-burn condition (5m and 1h windows above 14.4x
the budget). The payments dependency SLO opened no page, as designed. The
fault was found through the trace linked from the latency graph and cleared.

## Error budget

The 30-day budget for checkout availability is 0.1% of checkouts. At the
demo's ~1.5 checkouts/s, a month is ~3.9M checkouts and the budget ~3,900
failures. Ten minutes at 60% failure ≈ 540 failed checkouts, **~14% of the
monthly budget** in one incident. Budget left stays above 50%, so the
[error budget policy](../slo-policy.md#error-budget-policy) does not restrict
releases, but a second incident like this one in the same month would.

## Timeline (T = fault injected)

| Time | Event |
|---|---|
| T−10m | Baseline: checkout error ratio 0%, no alerts firing. |
| T+0 | 60% error rate injected into payments `/admin/faults`. |
| T+15s | First 502s at storefront; *Service RED* 5xx ratio for `/checkout` jumps. |
| T+31s | 5m ratio passes 1.44% (14.4 × 0.1%); the 1h ratio, computed over all data so far, also above. **`CheckoutAvailability` page fires.** |
| T+~45s | Alertmanager delivers to the `pager` receiver with the runbook link (`group_wait` 10s). |
| T+~1m | `PaymentsCaptureAvailability` evaluates as a **ticket only**; no second page. |
| T+3m | On-call opens the runbook. *Service RED*: 5xx at storefront (502), orders (502), payments (503). The deepest 5xx is payments. |
| T+4m | p99 exemplar → trace: payments span red, with event `fault.error`. Loki logs for the trace show `payment failed` in orders. |
| T+5m | `scripts/chaos.sh status payments` confirms an active fault. |
| T+10m | Fault cleared. Checkouts succeed again. |
| T+~15m | 5m window back below threshold; page resolves. |

## What went well

* **Detection was fast and at the right place.** The page came from the
  user-facing SLO, not from the dependency, so the on-call saw *what users feel*
  first. The dependency ticket existed but did not add a second page.
* **Correlation worked.** Metric → exemplar → trace → logs took under a minute
  and pointed at the failing hop without guessing.
* **The runbook had the right first query** (5xx by service) for a
  multi-hop failure.

## What went wrong

* **Errors were amplified, not contained.** Orders retried nothing and failed
  fast, which is correct, but storefront has no fallback for checkout (e.g.
  queue the order and capture payment later). Every payments failure became a
  user-facing failure.
* **The page resolves ~5 minutes after recovery**, because the short window is
  5 minutes. Expected behaviour, but it was not obvious to the responder; added
  to the runbook.
* **No deploy/change annotation on dashboards.** "What changed?" was answered by
  asking around. In a real incident that costs minutes.

## Action items

| # | Action | Type | Owner |
|---|---|---|---|
| 1 | Add change annotations (deploys, fault injection) to the Grafana dashboards | detect | shop |
| 2 | Evaluate async capture for checkout (accept order, capture later) to stop payments failures reaching users | mitigate | shop + payments |
| 3 | Document in the runbook that the page resolves a few minutes after recovery | process | shop — **done** |
| 4 | Disable `/admin/faults` unless `ADMIN_TOKEN` is set, outside the demo profile | prevent | platform |

## Lessons for the SLO design

The dependency SLO earned its place: without it, payments would have no
signal of its own about budget burn at low error rates, which are too small to
page at the edge. And keeping it ticket-only avoided a double page. Both
decisions are now covered by unit tests in
[`slo-alerts.test.yml`](../../observability/prometheus/tests/slo-alerts.test.yml).
