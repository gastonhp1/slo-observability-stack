"""Turn SLO specs into Prometheus rule groups.

Per SLO three groups are produced:

* ``slo-sli-recordings-<id>``  error ratio for every alerting window, plus the
  30d period (averaged from the 5m ratio).
* ``slo-meta-recordings-<id>`` objective, error budget, burn rates and the
  remaining budget for the period, for dashboards and the SLO table.
* ``slo-alerts-<id>``          the multiwindow multi-burn-rate page/ticket alerts.

Recording rule names follow the ``level:metric:operation`` convention and match
the ones Sloth generates (``slo:sli_error:ratio_rate5m`` ...), with ``slo_*``
labels instead of ``sloth_*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .spec import WINDOW_TOKEN, SLOSpec

SLI_WINDOWS = ("5m", "30m", "1h", "2h", "6h", "1d", "3d")
PERIOD = "30d"
PERIOD_DAYS = 30


@dataclass(frozen=True)
class BurnRateRule:
    severity: str
    short_window: str
    long_window: str
    factor: Decimal


# Google SRE Workbook, "Alerting on SLOs", recommended parameters for a 30d SLO.
PAGE_RULES = (
    BurnRateRule("page", "5m", "1h", Decimal("14.4")),
    BurnRateRule("page", "30m", "6h", Decimal("6")),
)
TICKET_RULES = (
    BurnRateRule("ticket", "2h", "1d", Decimal("3")),
    BurnRateRule("ticket", "6h", "3d", Decimal("1")),
)


def fmt(value: Decimal) -> str:
    """Plain decimal string without float noise or exponent: 0.001, 14.4, 99.95."""
    text = format(value.normalize(), "f")
    return text


def record_name(window: str) -> str:
    return f"slo:sli_error:ratio_rate{window}"


def selector(slo: SLOSpec) -> str:
    return f'{{slo_id="{slo.id}", slo_service="{slo.service}", slo_name="{slo.name}"}}'


def identity_labels(slo: SLOSpec) -> dict[str, str]:
    return {**slo.labels, "slo_id": slo.id, "slo_service": slo.service, "slo_name": slo.name}


def sli_expr(slo: SLOSpec, window: str) -> str:
    if slo.error_ratio_query:
        return slo.error_ratio_query.replace(WINDOW_TOKEN, window)
    assert slo.error_query and slo.total_query
    error = slo.error_query.replace(WINDOW_TOKEN, window)
    total = slo.total_query.replace(WINDOW_TOKEN, window)
    # A healthy service has no 5xx series at all, so the error query returns an
    # empty vector, not 0, and `empty / total` is empty too: the SLI would have
    # no data exactly when everything is fine. `or on() vector(0)` turns "no
    # errors" into 0 while still returning nothing when there is no traffic.
    return f"(\n  ({error})\n  or on() vector(0)\n)\n/\n(\n  {total}\n)"


def sli_group(slo: SLOSpec) -> dict:
    rules = [
        {"record": record_name(w), "expr": sli_expr(slo, w), "labels": {**identity_labels(slo), "slo_window": w}}
        for w in SLI_WINDOWS
    ]
    # Time-weighted average of the 5m ratio. Cheap and good enough for a period
    # view; it is not traffic-weighted (a quiet hour counts as much as a busy one).
    rules.append(
        {
            "record": record_name(PERIOD),
            "expr": f"avg_over_time({record_name('5m')}{selector(slo)}[{PERIOD}])",
            "labels": {**identity_labels(slo), "slo_window": PERIOD},
        }
    )
    return {"name": f"slo-sli-recordings-{slo.id}", "rules": rules}


def meta_group(slo: SLOSpec) -> dict:
    labels = identity_labels(slo)
    sel = selector(slo)
    on = "on (slo_id, slo_service, slo_name) group_left"
    rules = [
        {"record": "slo:objective:ratio", "expr": f"vector({fmt(slo.objective_ratio)})", "labels": labels},
        {"record": "slo:error_budget:ratio", "expr": f"vector({fmt(slo.error_budget_ratio)})", "labels": labels},
        {"record": "slo:time_period:days", "expr": f"vector({PERIOD_DAYS})", "labels": labels},
        {
            "record": "slo:current_burn_rate:ratio",
            "expr": f"{record_name('5m')}{sel}\n/ {on}\nslo:error_budget:ratio{sel}",
            "labels": labels,
        },
        {
            "record": "slo:period_burn_rate:ratio",
            "expr": f"{record_name(PERIOD)}{sel}\n/ {on}\nslo:error_budget:ratio{sel}",
            "labels": labels,
        },
        {
            "record": "slo:period_error_budget_remaining:ratio",
            "expr": f"1 - slo:period_burn_rate:ratio{sel}",
            "labels": labels,
        },
        {
            "record": "slo:info",
            "expr": "vector(1)",
            "labels": {**labels, "slo_objective": fmt(slo.objective), "slo_description": slo.description},
        },
    ]
    return {"name": f"slo-meta-recordings-{slo.id}", "rules": rules}


def burn_condition(slo: SLOSpec, rule: BurnRateRule) -> str:
    threshold = f"({fmt(rule.factor)} * {fmt(slo.error_budget_ratio)})"
    sel = selector(slo)
    return (
        f"(\n"
        f"  max({record_name(rule.short_window)}{sel} > {threshold}) without (slo_window)\n"
        f"  and\n"
        f"  max({record_name(rule.long_window)}{sel} > {threshold}) without (slo_window)\n"
        f")"
    )


def alert_rule(slo: SLOSpec, severity: str, burn_rules: tuple[BurnRateRule, ...]) -> dict | None:
    alerting = slo.alerting
    assert alerting is not None
    alert = alerting.page if severity == "page" else alerting.ticket
    if alert.disabled:
        return None
    windows = ", ".join(f"{r.short_window}/{r.long_window} @ {fmt(r.factor)}x" for r in burn_rules)
    annotations = {
        "summary": "{{ $labels.slo_service }}/{{ $labels.slo_name }}: error budget burning too fast " + f"({severity})",
        "description": (
            f"SLO {slo.id} (objective {fmt(slo.objective)}%) is burning its {PERIOD} error budget faster than "
            f"allowed for a {severity} ({windows}). Error ratio over the short window: "
            "{{ $value | humanizePercentage }}."
        ),
        **alerting.annotations,
        **alert.annotations,
    }
    return {
        "alert": alerting.name,
        "expr": "\nor\n".join(burn_condition(slo, r) for r in burn_rules),
        "labels": {**alerting.labels, "severity": severity, **alert.labels},
        "annotations": annotations,
    }


def alert_group(slo: SLOSpec) -> dict | None:
    if slo.alerting is None:
        return None
    rules = [r for r in (alert_rule(slo, "page", PAGE_RULES), alert_rule(slo, "ticket", TICKET_RULES)) if r]
    return {"name": f"slo-alerts-{slo.id}", "rules": rules} if rules else None


def generate(slos: list[SLOSpec]) -> dict:
    groups: list[dict] = []
    for slo in slos:
        groups.append(sli_group(slo))
        groups.append(meta_group(slo))
        alerts = alert_group(slo)
        if alerts:
            groups.append(alerts)
    return {"groups": groups}


def all_expressions(rule_file: dict) -> list[str]:
    return [rule["expr"] for group in rule_file["groups"] for rule in group["rules"]]
