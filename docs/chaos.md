# Chaos experiments

Every service exposes `/admin/faults` to inject errors or latency at runtime.
`scripts/chaos.sh` wraps it. Each experiment states its hypothesis up front, so
the result is a pass or fail of the alerting design, not just "things broke".

Set `ADMIN_TOKEN` in `.env` (and export it for the script) if the stack is
reachable by anyone else: with no token, `/admin/faults` is open.

## 1. Dependency failure pages at the edge, once

```bash
scripts/chaos.sh errors payments 0.6
```

**Hypothesis**
* `CheckoutAvailability` **page** fires within ~2 minutes (5m and 1h windows both
  above 14.4x the 0.1% budget).
* `PaymentsCaptureAvailability` does **not** page (dependency SLO, ticket only).
* The `CheckoutAvailability` ticket, if its condition is met, is **inhibited**
  by the page.
* Exactly one notification reaches the pager receiver:
  `curl -s localhost:8085/alerts?receiver=pager | jq`.
* Catalog is unaffected: `CatalogAvailability` stays quiet.

**Automated**: this is the scenario `tests/e2e/run.sh` runs in CI on every push.

## 2. Slow dependency burns the latency budget

```bash
scripts/chaos.sh latency payments 800 0.5
```

**Hypothesis**
* Half of the checkouts take > 500ms → `CheckoutLatency` page (50% bad events vs
  a 1% budget is a 50x burn).
* `CheckoutAvailability` stays quiet: 800ms is under the 2s downstream timeout,
  so requests are slow but succeed.
* On *Service RED* (storefront), p99 exemplars above the dashed 500ms line link
  to traces whose payments span carries a `fault.latency` event.

## 3. Latency over the timeout turns into errors

```bash
scripts/chaos.sh latency payments 2500 0.3
```

**Hypothesis**
* 30% of payments calls exceed the 2s client timeout in orders → orders returns
  504 → storefront returns 504.
* Both `CheckoutLatency` and `CheckoutAvailability` page. This is the expected
  coupling: a timeout is an error *and* a slow request.

## 4. Slow burn only opens a ticket

```bash
scripts/chaos.sh errors payments 0.004
```

**Hypothesis**
* 0.4% checkout errors = 4x burn. Above the 3x ticket threshold, below the 6x
  and 14.4x page thresholds: **ticket only, never a page**.
* Needs hours of data (2h and 1d windows), so it is verified in the promtool
  unit test `checkout slow burn opens a ticket but does not page` rather than
  live.

## 5. Dead service is caught by the symptom-less alert

```bash
docker compose stop payments
```

**Hypothesis**
* `ShopTargetDown` pages after 2 minutes for `payments:8080`.
* Checkout returns 502 (orders cannot reach payments), so `CheckoutAvailability`
  would also fire, but it is **inhibited** by `ShopTargetDown`.

Restore with `docker compose start payments`.

## Clean up

```bash
scripts/chaos.sh clear all
```
