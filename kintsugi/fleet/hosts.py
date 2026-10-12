"""The host registry: every ``hosts/<id>/host.yaml`` (spec §5.9)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict


class Slo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pass_rate: float
    p95_s: float | None = None


class Economics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: str
    price_usd_month: float
    value_per_failed_task_usd: float
    tasks_per_hour: float
    llm_budget_usd_month: float
    # Price per call of a paid external service (G4's cost per task = LLM spend + these).
    service_costs_usd: dict[str, float] = {}


class Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side_effect: bool = False
    model: bool = False
    external: str | None = None


class HostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    package: str
    description: str
    model: str
    slo: Slo
    economics: Economics
    eval: dict[str, Any]
    tools: dict[str, Tool]
    services: dict[str, str]
    offline_model: str
    fleet: bool = True  # False: not a traffic-serving host (hosts/omega, Omega's own replay suite)
    root: Path = Path()

    def side_effecting_tools(self) -> set[str]:
        return {name for name, tool in self.tools.items() if tool.side_effect}


def load_host(path: Path) -> HostSpec:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return HostSpec(**data, root=path.parent)


def load_hosts(repo: Path) -> dict[str, HostSpec]:
    specs = [load_host(p) for p in sorted((repo / "hosts").glob("*/host.yaml"))]
    return {spec.id: spec for spec in specs if spec.fleet}


def resolve(dotted: str) -> Any:
    """``package.module:attribute`` → the attribute."""
    module, _, attribute = dotted.partition(":")
    if not attribute:
        raise ValueError(f"expected module:attribute, got {dotted!r}")
    return getattr(importlib.import_module(module), attribute)
