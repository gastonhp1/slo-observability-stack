# Runbook: CatalogAvailability

| | |
|---|---|
| **Alert** | `CatalogAvailability` (page and ticket) |
| **SLO** | `storefront-catalog-availability`: 99.9% of `/products*` requests without 5xx over 30d |
| **Dashboards** | [Service RED](http://localhost:3000/d/service-red?var-service=storefront) |
| **Owner** | team `shop` |

## What it means

Product listing or product detail pages are failing with server errors. The
catalog is served by storefront alone (no downstream calls), so the cause is in
storefront itself.

## Triage

1. Which route? `/products` (listing) vs `/products/{product_id}` (detail):
   ```promql
   sum by (route, code) (rate(http_requests_total{service="storefront", route=~"/products.*"}[5m]))
   ```
2. Injected? `scripts/chaos.sh status storefront`. Faults on storefront affect
   every route, so `CheckoutAvailability` will usually fire at the same time.
3. Logs:
   ```logql
   {service_name="storefront"} | severity_text=~"(?i)error"
   ```
4. Recent deploy? `shop_build_info{service="storefront"}`.

## Mitigate

* Fault injection: `scripts/chaos.sh clear storefront`.
* Bad deploy: roll back.
* 404s are **not** part of this SLO. If users report "product not found", that
  is a catalog data problem, not an availability incident.
