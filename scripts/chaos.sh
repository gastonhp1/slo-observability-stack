#!/usr/bin/env bash
# Chaos experiments against the local stack, through /admin/faults.
#
#   scripts/chaos.sh errors   [service] [rate]     # default: payments 0.3
#   scripts/chaos.sh latency  [service] [ms] [rate] # default: payments 800 0.5
#   scripts/chaos.sh status   [service]
#   scripts/chaos.sh clear    [service|all]
#
# Each experiment is described in docs/chaos.md with the expected outcome.
set -euo pipefail

declare -A PORTS=([storefront]=8080 [orders]=8081 [payments]=8082)
HOST=${HOST:-localhost}
ADMIN_TOKEN=${ADMIN_TOKEN:-}

url_for() {
  local svc=$1
  [[ -n ${PORTS[$svc]:-} ]] || { echo "unknown service: $svc (storefront|orders|payments)" >&2; exit 2; }
  echo "http://$HOST:${PORTS[$svc]}/admin/faults"
}

call() {
  local method=$1 url=$2 body=${3:-}
  local args=(-fsS -X "$method" "$url")
  [[ -n $ADMIN_TOKEN ]] && args+=(-H "X-Admin-Token: $ADMIN_TOKEN")
  [[ -n $body ]] && args+=(-H 'content-type: application/json' -d "$body")
  curl "${args[@]}"
  echo
}

cmd=${1:-help}
case $cmd in
  errors)
    svc=${2:-payments} rate=${3:-0.3}
    call PUT "$(url_for "$svc")" "{\"error_rate\": $rate}"
    ;;
  latency)
    svc=${2:-payments} ms=${3:-800} rate=${4:-0.5}
    call PUT "$(url_for "$svc")" "{\"latency_ms\": $ms, \"latency_rate\": $rate}"
    ;;
  status)
    svc=${2:-payments}
    call GET "$(url_for "$svc")"
    ;;
  clear)
    target=${2:-all}
    if [[ $target == all ]]; then
      for svc in "${!PORTS[@]}"; do printf '%s: ' "$svc"; call DELETE "$(url_for "$svc")"; done
    else
      call DELETE "$(url_for "$target")"
    fi
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
