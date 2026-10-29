"""Spec model and validation.

The input format is the subset of Sloth's ``prometheus/v1`` spec that this
project uses, so the same files can be fed to ``sloth generate`` unchanged:

    version: prometheus/v1
    service: storefront
    labels: {team: shop}
    slos:
      - name: checkout-availability
        objective: 99.9
        description: ...
        sli:
          events:
            error_query: sum(rate(...{code=~"5.."}[{{.window}}]))
            total_query: sum(rate(...[{{.window}}]))
        alerting:
          name: CheckoutAvailability
          annotations: {runbook_url: ...}
          page_alert: {labels: {...}}
          ticket_alert: {disable: true}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

WINDOW_TOKEN = "{{.window}}"
SUPPORTED_VERSION = "prometheus/v1"
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
ALERT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class SpecError(ValueError):
    """A spec file is invalid. The message names the file and the field."""


@dataclass(frozen=True)
class AlertSpec:
    disabled: bool = False
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertingSpec:
    name: str
    labels: dict[str, str]
    annotations: dict[str, str]
    page: AlertSpec
    ticket: AlertSpec


@dataclass(frozen=True)
class SLOSpec:
    service: str
    name: str
    objective: Decimal  # percent, e.g. Decimal("99.9")
    description: str
    labels: dict[str, str]
    error_query: str | None
    total_query: str | None
    error_ratio_query: str | None
    alerting: AlertingSpec | None

    @property
    def id(self) -> str:
        return f"{self.service}-{self.name}"

    @property
    def objective_ratio(self) -> Decimal:
        return self.objective / Decimal(100)

    @property
    def error_budget_ratio(self) -> Decimal:
        # Decimal avoids 1 - 0.999 == 0.0010000000000000009 leaking into the rules.
        return Decimal(1) - self.objective_ratio


@dataclass(frozen=True)
class ServiceSpec:
    service: str
    labels: dict[str, str]
    slos: list[SLOSpec]
    source: Path | None = None


def _str_map(value: Any, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{where}: must be a mapping")
    out = {}
    for key, val in value.items():
        if not isinstance(key, str) or not LABEL_RE.match(key):
            raise SpecError(f"{where}: invalid label/annotation name {key!r}")
        out[key] = str(val)
    return out


def _alert(value: Any, where: str) -> AlertSpec:
    value = value or {}
    if not isinstance(value, dict):
        raise SpecError(f"{where}: must be a mapping")
    return AlertSpec(
        disabled=bool(value.get("disable", False)),
        labels=_str_map(value.get("labels"), f"{where}.labels"),
        annotations=_str_map(value.get("annotations"), f"{where}.annotations"),
    )


def _query(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{where}: required non-empty string")
    if WINDOW_TOKEN not in value:
        raise SpecError(f"{where}: must contain {WINDOW_TOKEN} so it can be evaluated per window")
    return " ".join(value.split())


def parse(data: dict, source: Path | None = None) -> ServiceSpec:
    where = str(source) if source else "<spec>"
    if not isinstance(data, dict):
        raise SpecError(f"{where}: top level must be a mapping")
    if data.get("version") != SUPPORTED_VERSION:
        raise SpecError(f"{where}: version must be {SUPPORTED_VERSION!r}, got {data.get('version')!r}")
    service = data.get("service")
    if not isinstance(service, str) or not NAME_RE.match(service):
        raise SpecError(f"{where}: service must match {NAME_RE.pattern}")
    service_labels = _str_map(data.get("labels"), f"{where}: labels")
    raw_slos = data.get("slos")
    if not isinstance(raw_slos, list) or not raw_slos:
        raise SpecError(f"{where}: slos must be a non-empty list")

    slos: list[SLOSpec] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_slos):
        at = f"{where}: slos[{i}]"
        if not isinstance(raw, dict):
            raise SpecError(f"{at}: must be a mapping")
        name = raw.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise SpecError(f"{at}.name must match {NAME_RE.pattern}")
        if name in seen:
            raise SpecError(f"{at}.name {name!r} is duplicated")
        seen.add(name)
        at = f"{where}: slo {name!r}"

        try:
            objective = Decimal(str(raw.get("objective")))
        except Exception as exc:  # noqa: BLE001 - re-raised with context
            raise SpecError(f"{at}: objective must be a number") from exc
        if not (Decimal(0) < objective < Decimal(100)):
            raise SpecError(f"{at}: objective must be > 0 and < 100 (percent), got {objective}")

        sli = raw.get("sli") or {}
        events, raw_sli = sli.get("events"), sli.get("raw")
        if bool(events) == bool(raw_sli):
            raise SpecError(f"{at}: sli needs exactly one of 'events' or 'raw'")
        error_query = total_query = ratio_query = None
        if events:
            error_query = _query(events.get("error_query"), f"{at}.sli.events.error_query")
            total_query = _query(events.get("total_query"), f"{at}.sli.events.total_query")
        else:
            ratio_query = _query(raw_sli.get("error_ratio_query"), f"{at}.sli.raw.error_ratio_query")

        alerting = None
        raw_alerting = raw.get("alerting")
        if raw_alerting:
            alert_name = raw_alerting.get("name")
            if not isinstance(alert_name, str) or not ALERT_NAME_RE.match(alert_name):
                raise SpecError(f"{at}.alerting.name must be a valid Prometheus alert name")
            alerting = AlertingSpec(
                name=alert_name,
                labels=_str_map(raw_alerting.get("labels"), f"{at}.alerting.labels"),
                annotations=_str_map(raw_alerting.get("annotations"), f"{at}.alerting.annotations"),
                page=_alert(raw_alerting.get("page_alert"), f"{at}.alerting.page_alert"),
                ticket=_alert(raw_alerting.get("ticket_alert"), f"{at}.alerting.ticket_alert"),
            )

        slos.append(
            SLOSpec(
                service=service,
                name=name,
                objective=objective,
                description=str(raw.get("description", "")).strip(),
                labels={**service_labels, **_str_map(raw.get("labels"), f"{at}.labels")},
                error_query=error_query,
                total_query=total_query,
                error_ratio_query=ratio_query,
                alerting=alerting,
            )
        )
    return ServiceSpec(service=service, labels=service_labels, slos=slos, source=source)


def load(path: Path) -> ServiceSpec:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError(f"{path}: invalid YAML: {exc}") from exc
    return parse(data, source=path)


def load_dir(directory: Path) -> list[ServiceSpec]:
    files = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
    if not files:
        raise SpecError(f"{directory}: no *.yml spec files found")
    specs = [load(f) for f in files]
    ids = [slo.id for spec in specs for slo in spec.slos]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SpecError(f"duplicated SLO ids across files: {sorted(dupes)}")
    return specs
