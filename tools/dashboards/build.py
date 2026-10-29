"""Builds the Grafana dashboards as code.

    python tools/dashboards/build.py          # write JSON
    python tools/dashboards/build.py --check  # CI: fail if the JSON on disk is stale

Dashboards are generated instead of hand-edited so panel ids, datasource
references and the SLO recording-rule names stay consistent. The output is
committed so Grafana provisioning needs no build step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "observability" / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}
SLO_SEL = 'slo_id=~"$slo"'


class Layout:
    """Places panels left to right on Grafana's 24-column grid."""

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.row_h = 0
        self.next_id = 1

    def place(self, panel: dict, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        panel["id"] = self.next_id
        self.next_id += 1
        self.x += w
        self.row_h = max(self.row_h, h)
        return panel

    def row(self, title: str) -> dict:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel = {"type": "row", "title": title, "collapsed": False, "panels": []}
        self.place(panel, 24, 1)
        self.x, self.y, self.row_h = 0, self.y + 1, 0
        return panel


def target(
    expr: str, legend: str = "", ref: str = "A", instant: bool = False, fmt: str = "time_series", exemplar: bool = False
) -> dict:
    return {
        "datasource": PROM,
        "refId": ref,
        "expr": expr,
        "legendFormat": legend or "__auto",
        "instant": instant,
        "range": not instant,
        "format": fmt,
        "exemplar": exemplar,
    }


def timeseries(
    title: str,
    targets: list[dict],
    unit: str = "short",
    description: str = "",
    thresholds: list[tuple[float, str]] | None = None,
    min_: float | None = None,
    max_: float | None = None,
) -> dict:
    defaults: dict = {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never"}}
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    if thresholds:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": "green", "value": None}] + [{"color": c, "value": v} for v, c in thresholds],
        }
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(
    title: str,
    expr: str,
    unit: str,
    thresholds: list[tuple[float | None, str]],
    description: str = "",
    decimals: int | None = None,
) -> dict:
    defaults: dict = {
        "unit": unit,
        "thresholds": {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in thresholds]},
        "color": {"mode": "thresholds"},
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": [target(expr, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value_and_name",
        },
    }


def slo_variable() -> dict:
    return {
        "name": "slo",
        "label": "SLO",
        "type": "query",
        "datasource": PROM,
        "query": {"query": "label_values(slo:info, slo_id)", "refId": "slo"},
        "definition": "label_values(slo:info, slo_id)",
        "refresh": 2,
        "multi": True,
        "includeAll": True,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
        "sort": 1,
    }


def dashboard(
    uid: str,
    title: str,
    description: str,
    panels: list[dict],
    variables: list[dict],
    tags: list[str],
    refresh: str = "30s",
    time_from: str = "now-1h",
) -> dict:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": tags,
        "editable": False,
        "graphTooltip": 1,
        "refresh": refresh,
        "schemaVersion": 41,
        "time": {"from": time_from, "to": "now"},
        "timezone": "browser",
        "templating": {"list": variables},
        "annotations": {
            "list": [
                {
                    "name": "SLO alerts",
                    "datasource": PROM,
                    "enable": True,
                    "iconColor": "red",
                    "expr": 'ALERTS{alertstate="firing", slo_id=~"$slo"}',
                    "titleFormat": "{{alertname}} ({{severity}})",
                    "textFormat": "{{slo_id}}",
                    "step": "30s",
                }
            ]
            if any(v["name"] == "slo" for v in variables)
            else []
        },
        "links": [
            {"title": "SLO overview", "type": "link", "url": "/d/slo-overview", "keepTime": True},
            {"title": "Service RED", "type": "link", "url": "/d/service-red", "keepTime": True},
        ],
        "panels": panels,
    }


def slo_overview() -> dict:
    lay = Layout()
    panels: list[dict] = []

    panels.append(lay.row("Where are we against the objective"))
    table = {
        "type": "table",
        "title": "SLOs",
        "description": "One row per SLO. Budget remaining is for the 30d period; burn rate 1 = spending the budget "
        "exactly by the end of the period.",
        "datasource": PROM,
        "targets": [
            target(f"slo:info{{{SLO_SEL}}}", ref="info", instant=True, fmt="table"),
            target(f"slo:objective:ratio{{{SLO_SEL}}}", ref="objective", instant=True, fmt="table"),
            target(f"1 - slo:sli_error:ratio_rate30d{{{SLO_SEL}}}", ref="sli", instant=True, fmt="table"),
            target(f"slo:period_error_budget_remaining:ratio{{{SLO_SEL}}}", ref="budget", instant=True, fmt="table"),
            target(f"slo:current_burn_rate:ratio{{{SLO_SEL}}}", ref="burn5m", instant=True, fmt="table"),
            target(
                f"slo:sli_error:ratio_rate1h{{{SLO_SEL}}} / on (slo_id) group_left slo:error_budget:ratio{{{SLO_SEL}}}",
                ref="burn1h",
                instant=True,
                fmt="table",
            ),
        ],
        "transformations": [
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "slo_window": True,
                        "team": False,
                        "tier": True,
                        "slo_service": True,
                        "slo_name": True,
                        "slo_description": True,
                        "slo_objective": True,
                        "Value #info": True,
                    },
                    "renameByName": {
                        "slo_id": "SLO",
                        "team": "Team",
                        "Value #objective": "Objective",
                        "Value #sli": "SLI (30d)",
                        "Value #budget": "Budget left (30d)",
                        "Value #burn5m": "Burn rate 5m",
                        "Value #burn1h": "Burn rate 1h",
                    },
                    "indexByName": {
                        "slo_id": 0,
                        "team": 1,
                        "Value #objective": 2,
                        "Value #sli": 3,
                        "Value #budget": 4,
                        "Value #burn5m": 5,
                        "Value #burn1h": 6,
                    },
                },
            },
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto"}},
            "overrides": [
                {
                    "matcher": {"id": "byRegexp", "options": "Objective|SLI.*"},
                    "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 3}],
                },
                {
                    "matcher": {"id": "byName", "options": "Budget left (30d)"},
                    "properties": [
                        {"id": "unit", "value": "percentunit"},
                        {"id": "decimals", "value": 1},
                        {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                        {
                            "id": "thresholds",
                            "value": {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "red", "value": None},
                                    {"color": "orange", "value": 0.25},
                                    {"color": "green", "value": 0.5},
                                ],
                            },
                        },
                    ],
                },
                {
                    "matcher": {"id": "byRegexp", "options": "Burn rate.*"},
                    "properties": [
                        {"id": "unit", "value": "x"},
                        {"id": "decimals", "value": 1},
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                        {
                            "id": "thresholds",
                            "value": {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "green", "value": None},
                                    {"color": "orange", "value": 1},
                                    {"color": "red", "value": 6},
                                ],
                            },
                        },
                    ],
                },
            ],
        },
        "options": {"showHeader": True, "sortBy": [{"displayName": "Budget left (30d)", "desc": False}]},
    }
    panels.append(lay.place(table, 24, 7))

    panels.append(lay.row("How fast is the budget going"))
    panels.append(
        lay.place(
            timeseries(
                "Burn rate (5m and 1h windows)",
                [
                    target(f"slo:current_burn_rate:ratio{{{SLO_SEL}}}", "{{slo_id}} 5m", "A"),
                    target(
                        f"slo:sli_error:ratio_rate1h{{{SLO_SEL}}} / on (slo_id) group_left slo:error_budget:ratio",
                        "{{slo_id}} 1h",
                        "B",
                    ),
                ],
                unit="x",
                description="Dashed lines: 6x and 14.4x, the page thresholds. A page needs BOTH the short and the long "
                "window above the line, which is what filters out short spikes.",
                thresholds=[(6, "orange"), (14.4, "red")],
                min_=0,
            ),
            12,
            9,
        )
    )
    panels.append(
        lay.place(
            timeseries(
                "Error budget remaining (30d)",
                [target(f"slo:period_error_budget_remaining:ratio{{{SLO_SEL}}}", "{{slo_id}}")],
                unit="percentunit",
                description="Below 0 the SLO is breached for the period: the error budget policy freezes risky changes.",
                thresholds=[(0, "red")],
            ),
            12,
            9,
        )
    )

    panels.append(lay.row("Signals behind the SLIs"))
    panels.append(
        lay.place(
            timeseries(
                "Error ratio by window",
                [
                    target(f"slo:sli_error:ratio_rate{w}{{{SLO_SEL}}}", f"{{{{slo_id}}}} {w}", chr(65 + i))
                    for i, w in enumerate(("5m", "1h", "6h"))
                ],
                unit="percentunit",
                min_=0,
            ),
            12,
            8,
        )
    )
    alerts = {
        "type": "table",
        "title": "Firing SLO and platform alerts",
        "datasource": PROM,
        "targets": [target('ALERTS{alertstate="firing"}', instant=True, fmt="table")],
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True, "Value": True, "__name__": True, "alertstate": True},
                    "indexByName": {"alertname": 0, "severity": 1, "slo_id": 2},
                },
            }
        ],
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "severity"},
                    "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                        {
                            "id": "mappings",
                            "value": [
                                {
                                    "type": "value",
                                    "options": {
                                        "page": {"color": "red", "index": 0},
                                        "ticket": {"color": "orange", "index": 1},
                                    },
                                }
                            ],
                        },
                    ],
                }
            ],
        },
        "options": {"showHeader": True},
    }
    panels.append(lay.place(alerts, 12, 8))

    return dashboard(
        "slo-overview",
        "SLO overview",
        "Error budgets and burn rates for every SLO defined in slo/specs. Generated by tools/dashboards/build.py.",
        panels,
        [slo_variable()],
        ["slo", "sre"],
        time_from="now-6h",
    )


def service_red() -> dict:
    lay = Layout()
    sel = 'service="$service"'
    panels: list[dict] = []

    panels.append(lay.row("Now"))
    panels.append(
        lay.place(stat("Requests / s", f"sum(rate(http_requests_total{{{sel}}}[5m]))", "reqps", [(None, "blue")]), 6, 4)
    )
    panels.append(
        lay.place(
            stat(
                "5xx ratio (5m)",
                f'sum(rate(http_requests_total{{{sel}, code=~"5.."}}[5m])) / sum(rate(http_requests_total{{{sel}}}[5m]))',
                "percentunit",
                [(None, "green"), (0.001, "orange"), (0.01, "red")],
                decimals=2,
            ),
            6,
            4,
        )
    )
    panels.append(
        lay.place(
            stat(
                "p99 latency (5m)",
                f"histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[5m])))",
                "s",
                [(None, "green"), (0.3, "orange"), (0.5, "red")],
            ),
            6,
            4,
        )
    )
    panels.append(
        lay.place(
            stat(
                "Faults injected / s",
                f"sum(rate(shop_faults_injected_total{{{sel}}}[5m])) or vector(0)",
                "reqps",
                [(None, "green"), (0.0001, "purple")],
                description="Non-zero means a chaos experiment is running against this service.",
            ),
            6,
            4,
        )
    )

    panels.append(lay.row("Rate, errors, duration by route"))
    panels.append(
        lay.place(
            timeseries(
                "Requests / s by route and code",
                [target(f"sum by (route, code) (rate(http_requests_total{{{sel}}}[1m]))", "{{route}} {{code}}")],
                unit="reqps",
                min_=0,
            ),
            12,
            8,
        )
    )
    panels.append(
        lay.place(
            timeseries(
                "5xx ratio by route",
                [
                    target(
                        f'sum by (route) (rate(http_requests_total{{{sel}, code=~"5.."}}[1m]))'
                        f" / sum by (route) (rate(http_requests_total{{{sel}}}[1m]))",
                        "{{route}}",
                    )
                ],
                unit="percentunit",
                min_=0,
            ),
            12,
            8,
        )
    )
    panels.append(
        lay.place(
            timeseries(
                "Latency percentiles (click an exemplar dot to open the trace)",
                [
                    target(
                        f"histogram_quantile({q}, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[1m])))",
                        f"p{int(q * 100)}",
                        chr(65 + i),
                        exemplar=(q == 0.99),
                    )
                    for i, q in enumerate((0.5, 0.95, 0.99))
                ],
                unit="s",
                min_=0,
                thresholds=[(0.5, "red")],
                description="Dashed line: the 500ms checkout latency SLO threshold.",
            ),
            24,
            9,
        )
    )

    panels.append(lay.row("Logs and traces"))
    panels.append(
        lay.place(
            {
                "type": "logs",
                "title": "Warnings and errors",
                "datasource": LOKI,
                "targets": [
                    {
                        "datasource": LOKI,
                        "refId": "A",
                        "expr": '{service_name="$service"} | severity_text=~"(?i)(warn|warning|error|critical)"',
                    }
                ],
                "options": {
                    "showTime": True,
                    "wrapLogMessage": True,
                    "enableLogDetails": True,
                    "sortOrder": "Descending",
                    "dedupStrategy": "none",
                },
            },
            12,
            10,
        )
    )
    panels.append(
        lay.place(
            {
                "type": "table",
                "title": "Recent error traces",
                "datasource": TEMPO,
                "targets": [
                    {
                        "datasource": TEMPO,
                        "refId": "A",
                        "queryType": "traceql",
                        "limit": 20,
                        "tableType": "traces",
                        "query": '{resource.service.name="$service" && status=error}',
                    }
                ],
                "options": {"showHeader": True},
            },
            12,
            10,
        )
    )

    service_var = {
        "name": "service",
        "label": "Service",
        "type": "query",
        "datasource": PROM,
        "query": {"query": "label_values(http_requests_total, service)", "refId": "service"},
        "definition": "label_values(http_requests_total, service)",
        "refresh": 2,
        "current": {"selected": True, "text": "storefront", "value": "storefront"},
        "sort": 1,
    }
    return dashboard(
        "service-red",
        "Service RED",
        "Rate, errors and duration per service, with exemplars, logs and traces. Generated by "
        "tools/dashboards/build.py.",
        panels,
        [service_var],
        ["red", "services"],
    )


DASHBOARDS = {"slo-overview.json": slo_overview, "service-red.json": service_red}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Grafana dashboards")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, build in DASHBOARDS.items():
        content = json.dumps(build(), indent=2, sort_keys=False) + "\n"
        path = args.out / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path))
        else:
            path.write_text(content, encoding="utf-8")
            print(f"wrote {path}")
    if stale:
        sys.stderr.write("Stale dashboards (run `make dashboards`): " + ", ".join(stale) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
