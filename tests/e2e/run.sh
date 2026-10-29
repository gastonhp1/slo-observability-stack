#!/usr/bin/env bash
# End-to-end check of the whole stack, as run in CI after `docker compose up`.
#
#  1. every component is up and Prometheus scrapes the services
#  2. the SLO recording rules produce data and nothing is firing
#  3. inject 5xx into payments (a dependency, two hops from the user)
#  4. the user-facing CheckoutAvailability page fires, is routed by
#     Alertmanager to the pager receiver and reaches the webhook
#  5. the incident is visible in all three signals: exemplars, traces, logs
#  6. Grafana has its datasources and dashboards provisioned
#
# Needs: bash, curl, jq. Ports as published in docker-compose.yml.
set -euo pipefail

STOREFRONT=${STOREFRONT:-http://localhost:8080}
PAYMENTS=${PAYMENTS:-http://localhost:8082}
ALERT_SINK=${ALERT_SINK:-http://localhost:8085}
PROM=${PROM:-http://localhost:9090}
AM=${AM:-http://localhost:9093}
LOKI=${LOKI:-http://localhost:3100}
TEMPO=${TEMPO:-http://localhost:3200}
GRAFANA=${GRAFANA:-http://localhost:3000}
GRAFANA_AUTH=${GRAFANA_AUTH:-admin:admin}
ADMIN_TOKEN=${ADMIN_TOKEN:-}
FAULT_ERROR_RATE=${FAULT_ERROR_RATE:-0.6}

START=$(date +%s)
log()  { printf '[e2e +%4ss] %s\n' "$(( $(date +%s) - START ))" "$*"; }
# In GitHub Actions, results also become annotations on the run summary.
notice() { log "$*"; [[ -n ${GITHUB_ACTIONS:-} ]] && echo "::notice title=e2e::$*"; return 0; }
fail() {
  log "FAIL: $*"
  [[ -n ${GITHUB_ACTIONS:-} ]] && echo "::error title=e2e failed::$*"
  exit 1
}

# wait_for <description> <timeout seconds> <command...>: retries every 5s.
wait_for() {
  local what=$1 timeout=$2; shift 2
  local deadline=$(( $(date +%s) + timeout ))
  until "$@" >/dev/null 2>&1; do
    (( $(date +%s) < deadline )) || fail "timed out after ${timeout}s waiting for: $what"
    sleep 5
  done
  log "ok: $what"
}

prom_query() { curl -fsS --get "$PROM/api/v1/query" --data-urlencode "query=$1"; }
prom_has_result() { prom_query "$1" | jq -e '.data.result | length > 0'; }
prom_value() { prom_query "$1" | jq -r '.data.result[0].value[1] // empty'; }

admin_headers=()
[[ -n $ADMIN_TOKEN ]] && admin_headers=(-H "X-Admin-Token: $ADMIN_TOKEN")
set_fault() {
  curl -fsS -X PUT "$PAYMENTS/admin/faults" "${admin_headers[@]}" \
    -H 'content-type: application/json' -d "$1" >/dev/null
}
clear_fault() { curl -fsS -X DELETE "$PAYMENTS/admin/faults" "${admin_headers[@]}" >/dev/null || true; }
trap clear_fault EXIT

# --- 1. components up ------------------------------------------------------
wait_for "storefront ready"   120 curl -fsS "$STOREFRONT/readyz"
wait_for "alert-sink ready"   120 curl -fsS "$ALERT_SINK/healthz"
wait_for "prometheus ready"   120 curl -fsS "$PROM/-/ready"
wait_for "alertmanager ready" 120 curl -fsS "$AM/-/ready"
wait_for "loki ready"         180 curl -fsS "$LOKI/ready"
wait_for "tempo ready"        180 curl -fsS "$TEMPO/ready"
wait_for "grafana healthy"    120 curl -fsS "$GRAFANA/api/health"
wait_for "3 shop targets up"  120 bash -c "curl -fsS --get '$PROM/api/v1/query' --data-urlencode 'query=sum(up{job=\"shop\"}) == 3' | jq -e '.data.result | length == 1'"

rule_errors=$(curl -fsS "$PROM/api/v1/rules" | jq '[.data.groups[].rules[] | select(.health == "err")] | length')
[[ $rule_errors == 0 ]] || fail "$rule_errors Prometheus rules are unhealthy"
log "ok: all Prometheus rules healthy"

# --- 2. baseline -----------------------------------------------------------
wait_for "checkout SLI has data" 180 prom_has_result 'slo:sli_error:ratio_rate5m{slo_id="storefront-checkout-availability"}'
baseline=$(prom_value 'slo:sli_error:ratio_rate5m{slo_id="storefront-checkout-availability"}')
log "baseline checkout error ratio (5m): ${baseline}"
if prom_has_result 'ALERTS{alertname="CheckoutAvailability", alertstate="firing"}'; then
  fail "CheckoutAvailability is already firing before any fault was injected"
fi
log "ok: no SLO page firing at baseline"

# --- 3. inject the fault ---------------------------------------------------
log "injecting error_rate=${FAULT_ERROR_RATE} into payments"
set_fault "{\"error_rate\": ${FAULT_ERROR_RATE}}"
fault_at=$(date +%s)

# --- 4. the page fires and is delivered -------------------------------------
wait_for "CheckoutAvailability page firing in Prometheus" 300 \
  prom_has_result 'ALERTS{alertname="CheckoutAvailability", severity="page", alertstate="firing"}'
notice "fault injected -> CheckoutAvailability page firing in $(( $(date +%s) - fault_at ))s"

wait_for "page active in Alertmanager" 120 bash -c \
  "curl -fsS --get '$AM/api/v2/alerts' --data-urlencode 'filter=alertname=\"CheckoutAvailability\"' --data-urlencode 'filter=severity=\"page\"' --data-urlencode 'active=true' | jq -e 'length > 0'"

wait_for "page delivered to the pager receiver" 120 bash -c \
  "curl -fsS '$ALERT_SINK/alerts?receiver=pager&alertname=CheckoutAvailability' | jq -e 'map(select(.status == \"firing\")) | length > 0'"

notified=$(curl -fsS "$ALERT_SINK/alerts?receiver=pager&alertname=CheckoutAvailability" | jq -c '.[0] | {severity, slo, runbook: .annotations.runbook_url}')
notice "pager notification: $notified"
jq -e '.runbook | test("docs/runbooks/checkout-availability.md$")' <<<"$notified" >/dev/null \
  || fail "page has no runbook_url"

# While the SLO pages, its ticket is inhibited (one incident, one notification).
# Informational: the ticket condition (2h/1d windows) may not be met this early.
suppressed=$(curl -fsS --get "$AM/api/v2/alerts" \
  --data-urlencode 'filter=alertname="CheckoutAvailability"' --data-urlencode 'filter=severity="ticket"' \
  | jq '[.[] | select(.status.state == "suppressed")] | length')
log "inhibited CheckoutAvailability tickets: ${suppressed}"

# --- 5. signals ------------------------------------------------------------
wait_for "exemplars with trace_id on the latency histogram" 120 bash -c \
  "curl -fsS --get '$PROM/api/v1/query_exemplars' --data-urlencode 'query=http_request_duration_seconds_bucket{service=\"storefront\"}' --data-urlencode \"start=\$(( \$(date +%s) - 600 ))\" --data-urlencode \"end=\$(date +%s)\" | jq -e '[.data[].exemplars[].labels.trace_id] | length > 0'"

traceql='{resource.service.name="storefront"} && {resource.service.name="payments" && status=error}'
wait_for "Tempo has a failed checkout trace spanning storefront -> payments" 180 bash -c \
  "curl -fsS --get '$TEMPO/api/search' --data-urlencode 'q=$traceql' --data-urlencode 'limit=5' --data-urlencode \"start=\$(( \$(date +%s) - 900 ))\" --data-urlencode \"end=\$(date +%s)\" | jq -e '.traces | length > 0'"

wait_for "Loki has payments logs" 120 bash -c \
  "curl -fsS --get '$LOKI/loki/api/v1/query_range' --data-urlencode 'query={service_name=\"payments\"}' --data-urlencode 'limit=5' --data-urlencode 'since=15m' | jq -e '.data.result | length > 0'"

wait_for "Loki error logs carry trace_id (log -> trace correlation)" 120 bash -c \
  "curl -fsS --get '$LOKI/loki/api/v1/query_range' --data-urlencode 'query={service_name=\"storefront\"} | trace_id != \"\" | severity_text=~\"(?i)error\"' --data-urlencode 'limit=5' --data-urlencode 'since=15m' | jq -e '.data.result | length > 0'"

# --- 6. Grafana ------------------------------------------------------------
for uid in prometheus loki tempo; do
  wait_for "Grafana datasource '$uid' healthy" 60 bash -c \
    "curl -fsS -u '$GRAFANA_AUTH' '$GRAFANA/api/datasources/uid/$uid/health' | jq -e '.status == \"OK\"'"
done
for uid in slo-overview service-red; do
  wait_for "Grafana dashboard '$uid' provisioned" 60 bash -c \
    "curl -fsS -u '$GRAFANA_AUTH' '$GRAFANA/api/dashboards/uid/$uid' | jq -e '.dashboard.uid == \"$uid\"'"
done

# --- recovery ----------------------------------------------------------------
clear_fault
log "fault cleared"
wait_for "storefront serves checkouts again" 60 bash -c \
  "curl -fsS -X POST '$STOREFRONT/checkout' -H 'content-type: application/json' -d '{\"product_id\":\"p-100\",\"quantity\":1}' | jq -e '.status == \"confirmed\"'"

notice "PASSED in $(( $(date +%s) - START ))s"
