# SLOs and error budget policy

This is the agreement the alerts implement. If the policy and the rules ever
disagree, the rules are wrong.

## The SLOs

| SLO | Service | Objective (30d) | Error budget | Why this number |
|---|---|---|---|---|
| `checkout-availability` | storefront | 99.9% non-5xx | 0.1% ≈ 43 min of full outage | Checkout is revenue. A failed checkout is a lost order. |
| `checkout-latency` | storefront | 99% under 500ms | 1% of checkouts | Users notice > 0.5s on a "Pay" button; 1% slow is tolerable. |
| `catalog-availability` | storefront | 99.9% non-5xx | 0.1% | Browsing is the entry point; a broken catalog means no checkouts later. |
| `capture-availability` | payments | 99.95% non-5xx | 0.05% | Tighter than checkout: payments sits on the critical path of every order. |

The specs live in [`slo/specs/`](../slo/specs). Prometheus rules are generated
from them by [`slogen`](../tools/slogen), and CI fails if they drift.

### What counts as an error

* **5xx is an error, 4xx is not.** A declined payment (402) or an unknown
  product (404) is the system working as designed. Counting them would make the
  SLO page on user behaviour. The `client errors do not burn budget` unit test
  in [`slo-alerts.test.yml`](../observability/prometheus/tests/slo-alerts.test.yml)
  guards this.
* **Measured at the edge.** Availability of checkout is measured at storefront,
  where users feel it, not as the product of each dependency's availability.
* **Operational endpoints are excluded** (`/healthz`, `/readyz`, `/metrics`,
  `/admin/*`). They never reach the `http_requests_total` series.

## Alerting: multiwindow, multi-burn-rate

From the Google SRE Workbook, *Alerting on SLOs*. A **burn rate** of 1 spends
exactly the whole budget in 30 days; 14.4 spends it in about 2 days.

| Severity | Short window | Long window | Burn rate | Budget spent when it fires |
|---|---|---|---|---|
| page | 5m | 1h | 14.4x | 2% |
| page | 30m | 6h | 6x | 5% |
| ticket | 2h | 1d | 3x | 10% |
| ticket | 6h | 3d | 1x | 10% |

Both windows must exceed the threshold. The long window gives significance (a
10-second spike will not page); the short one makes the alert reset quickly
after the fix instead of firing for another hour.

### Routing

* `severity=page` → **pager** receiver: somebody is interrupted now.
* `severity=ticket` → **tickets** receiver: work for the next business day.
* While an SLO pages, its own ticket is **inhibited** (one incident, one notification).
* `ShopTargetDown` inhibits the availability SLO alerts: a dead process is the
  explanation, not a second incident.
* **Dependency SLOs do not page.** `capture-availability` only opens tickets. A
  payments outage already pages through `checkout-availability`; a second page
  for the same incident teaches people to ignore pages.

### What the SLO alerts cannot see

A service that is completely down emits *no* metrics, so its error ratio is
"no data", not 100%. Two hand-written alerts in
[`platform.yml`](../observability/prometheus/rules/platform.yml) cover that:
`ShopTargetDown` (page) and `SLIMetricsMissing` (ticket).

## Error budget policy

| Budget left (30d) | What happens |
|---|---|
| > 50% | Normal. Ship freely; this is the budget for experiments and risky changes. |
| 25–50% | Releases need a rollback plan written in the PR. Chaos experiments only in business hours. |
| 0–25% | Only fixes and reliability work merge to the affected service. |
| < 0 (breached) | Feature freeze for the service until the 30d SLI is back above objective. A postmortem is mandatory. |

Exceptions are decided by the service owner and the on-call lead together, and
written down in the postmortem or the PR.

A budget-exhausting incident caused by a dependency still counts against the
SLO (users do not care whose fault it was), but the freeze applies to the team
that owns the cause.

## Reviewing the SLOs

Every quarter, or after any postmortem that touches them, check:

1. Did every page in the period correspond to real user pain? If not, the
   objective is too tight or the SLI measures the wrong thing.
2. Was there user pain that did not page? Then the SLI is missing a failure mode.
3. Is the budget always ~100% left? The objective may be looser than what users
   actually get, and a regression would go unnoticed for too long.
