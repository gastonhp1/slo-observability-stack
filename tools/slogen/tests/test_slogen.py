from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import promql_parser
import pytest
import yaml

from slogen import cli, rules, spec

REPO = Path(__file__).resolve().parents[3]

BASE = {
    "version": "prometheus/v1",
    "service": "checkout",
    "labels": {"team": "shop"},
    "slos": [
        {
            "name": "availability",
            "objective": 99.9,
            "description": "no 5xx",
            "sli": {
                "events": {
                    "error_query": 'sum(rate(http_requests_total{code=~"5.."}[{{.window}}]))',
                    "total_query": "sum(rate(http_requests_total[{{.window}}]))",
                }
            },
            "alerting": {
                "name": "CheckoutAvailability",
                "annotations": {"runbook_url": "https://example.test/rb"},
                "page_alert": {"labels": {"routing_key": "oncall"}},
                "ticket_alert": {"labels": {"routing_key": "backlog"}},
            },
        }
    ],
}


def make(**overrides) -> dict:
    data = yaml.safe_load(yaml.safe_dump(BASE))  # deep copy
    data["slos"][0].update(overrides)
    return data


def rules_by_name(rule_file: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for group in rule_file["groups"]:
        for rule in group["rules"]:
            out.setdefault(rule.get("record") or rule["alert"], []).append(rule)
    return out


# --- spec validation -------------------------------------------------------


def test_error_budget_uses_exact_decimals():
    slo = spec.parse(make(objective=99.9)).slos[0]
    assert slo.error_budget_ratio == Decimal("0.001")
    assert rules.fmt(slo.error_budget_ratio) == "0.001"
    assert rules.fmt(spec.parse(make(objective=99.95)).slos[0].error_budget_ratio) == "0.0005"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"objective": 100}, "objective"),
        ({"objective": 0}, "objective"),
        ({"objective": "high"}, "objective"),
        ({"name": "Not Valid"}, "name"),
        (
            {"sli": {"events": {"error_query": "sum(rate(x[5m]))", "total_query": "sum(rate(y[{{.window}}]))"}}},
            "{{.window}}",
        ),
        ({"sli": {}}, "exactly one"),
        ({"alerting": {"name": "bad-name"}}, "alert name"),
    ],
)
def test_invalid_specs_are_rejected(overrides, message):
    with pytest.raises(spec.SpecError, match=message.replace("{", r"\{").replace(".", r"\.")):
        spec.parse(make(**overrides))


def test_wrong_version_is_rejected():
    data = make()
    data["version"] = "sloth.slok.dev/v1"
    with pytest.raises(spec.SpecError, match="version"):
        spec.parse(data)


def test_duplicated_slo_names_are_rejected():
    data = make()
    data["slos"].append(data["slos"][0])
    with pytest.raises(spec.SpecError, match="duplicated"):
        spec.parse(data)


# --- generated rules -------------------------------------------------------


def test_one_sli_recording_per_window_plus_period():
    out = rules.generate(spec.parse(make()).slos)
    by_name = rules_by_name(out)
    for window in (*rules.SLI_WINDOWS, rules.PERIOD):
        (rule,) = by_name[f"slo:sli_error:ratio_rate{window}"]
        assert rule["labels"]["slo_window"] == window
        assert rule["labels"]["slo_id"] == "checkout-availability"
        assert rule["labels"]["team"] == "shop"
    assert "[1h]" in by_name["slo:sli_error:ratio_rate1h"][0]["expr"]
    assert "{{.window}}" not in yaml.safe_dump(out)


def test_page_and_ticket_alerts_use_workbook_burn_rates():
    out = rules.generate(spec.parse(make()).slos)
    page, ticket = rules_by_name(out)["CheckoutAvailability"]
    assert page["labels"]["severity"] == "page"
    assert page["labels"]["routing_key"] == "oncall"
    assert "ratio_rate5m" in page["expr"] and "ratio_rate1h" in page["expr"]
    assert "(14.4 * 0.001)" in page["expr"] and "(6 * 0.001)" in page["expr"]
    assert ticket["labels"]["severity"] == "ticket"
    assert "(3 * 0.001)" in ticket["expr"] and "(1 * 0.001)" in ticket["expr"]
    assert page["annotations"]["runbook_url"] == "https://example.test/rb"


def test_disabled_page_alert_is_not_generated():
    data = make()
    data["slos"][0]["alerting"]["page_alert"] = {"disable": True}
    alerts = rules_by_name(rules.generate(spec.parse(data).slos))["CheckoutAvailability"]
    assert [a["labels"]["severity"] for a in alerts] == ["ticket"]


def test_no_alerting_section_means_no_alert_group():
    data = make()
    del data["slos"][0]["alerting"]
    out = rules.generate(spec.parse(data).slos)
    assert not any(g["name"].startswith("slo-alerts-") for g in out["groups"])


def test_raw_error_ratio_query_is_supported():
    data = make(sli={"raw": {"error_ratio_query": "1 - avg_over_time(probe_success[{{.window}}])"}})
    out = rules.generate(spec.parse(data).slos)
    assert rules_by_name(out)["slo:sli_error:ratio_rate6h"][0]["expr"] == "1 - avg_over_time(probe_success[6h])"


def test_every_generated_expression_is_valid_promql():
    out = rules.generate(spec.parse(make()).slos)
    for expr in rules.all_expressions(out):
        promql_parser.parse(expr)


@pytest.mark.parametrize(
    ("factor", "window_h", "budget_spent"), [(14.4, 1, 0.02), (6, 6, 0.05), (3, 24, 0.10), (1, 72, 0.10)]
)
def test_burn_rate_table_matches_budget_consumption(factor, window_h, budget_spent):
    """Sanity check of the table in the docs: burning at `factor` for the long
    window consumes `budget_spent` of a 30-day budget."""
    assert factor * window_h / (30 * 24) == pytest.approx(budget_spent, rel=0.01)


# --- CLI ------------------------------------------------------------------


def test_cli_generate_then_check_is_clean(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "checkout.yml").write_text(yaml.safe_dump(make()))
    out = tmp_path / "rules"
    assert cli.main(["generate", "--specs", str(specs), "--out", str(out)]) == 0
    assert cli.main(["generate", "--specs", str(specs), "--out", str(out), "--check"]) == 0
    text = (out / "checkout.yml").read_text()
    assert text.startswith("# Code generated by slogen")
    assert yaml.safe_load(text)["groups"]


def test_cli_check_detects_drift_and_stale_files(tmp_path, capsys):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "checkout.yml").write_text(yaml.safe_dump(make()))
    out = tmp_path / "rules"
    cli.main(["generate", "--specs", str(specs), "--out", str(out)])
    (specs / "checkout.yml").write_text(yaml.safe_dump(make(objective=99.5)))
    (out / "orphan.yml").write_text("groups: []\n")
    assert cli.main(["generate", "--specs", str(specs), "--out", str(out), "--check"]) == 1
    err = capsys.readouterr().err
    assert "0.005" in err and "orphan.yml" in err


def test_cli_reports_spec_errors(tmp_path, capsys):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "bad.yml").write_text(yaml.safe_dump(make(objective=101)))
    assert cli.main(["validate", "--specs", str(specs)]) == 2
    assert "objective" in capsys.readouterr().err


def test_repository_rules_are_up_to_date():
    """The committed rules must match the committed specs (same check as CI)."""
    assert (
        cli.main(
            [
                "generate",
                "--specs",
                str(REPO / "slo" / "specs"),
                "--out",
                str(REPO / "observability" / "prometheus" / "rules" / "slo"),
                "--check",
            ]
        )
        == 0
    )
