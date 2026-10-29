# Runbook: PaymentsCaptureAvailability

| | |
|---|---|
| **Alert** | `PaymentsCaptureAvailability` (ticket only, by design) |
| **SLO** | `payments-capture-availability`: 99.95% of `POST /payments` without 5xx over 30d |
| **Dashboards** | [Service RED](http://localhost:3000/d/service-red?var-service=payments) |
| **Owner** | team `payments` |

## Why it never pages

Payments is a dependency. When it fails badly, users feel it at checkout and
`CheckoutAvailability` pages the shop on-call. A second page for the same
incident would be noise. This ticket exists so the payments team learns its
budget is going even when the impact is too small to page at the edge.

## What to do

1. Check if there is an active `CheckoutAvailability` page. If yes, join that
   incident; this ticket is the same problem seen from the dependency.
2. Otherwise, look at the error pattern over the ticket windows (1d and 3d):
   ```promql
   sum by (code) (increase(http_requests_total{service="payments", route="/payments"}[1d]))
   ```
3. Find failed captures:
   ```traceql
   {resource.service.name="payments" && status=error}
   ```
4. Plan the fix as normal work. If the 30d budget left is under 25%, the
   [error budget policy](../slo-policy.md#error-budget-policy) restricts payments releases to fixes.

`402 payment declined` responses are business outcomes and do not burn this budget.
