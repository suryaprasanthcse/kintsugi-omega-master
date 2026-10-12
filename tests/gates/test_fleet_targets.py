"""The fleet's repair targets (``.kintsugi/target.yaml``): every host the crew may repair or
vaccinate resolves to real code, tests, a sealed holdout and its declared live services."""

from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi.gates.target import load_target, target_hosts

OMEGA = Path(__file__).resolve().parents[2]


def test_the_fleet_is_declared() -> None:
    assert target_hosts(OMEGA) == ["ledger", "omega", "scout", "triage"]


def test_omega_itself_is_the_self_repair_target() -> None:
    target = load_target(OMEGA, "omega")
    assert target.self_repair
    assert target.scopes == ("surgeon", "self_repair")
    assert not target.host.fleet  # never serves traffic, never vaccinated
    code = Path(target.code_env(OMEGA)["PYTHONPATH"])
    assert (code / "omega" / "evals" / "cases.py").is_file()
    assert all(not load_target(OMEGA, h).self_repair for h in ("ledger", "scout", "triage"))


@pytest.mark.parametrize("host", ["ledger", "scout", "triage"])
def test_each_target_resolves(host: str) -> None:
    target = load_target(OMEGA, host)
    assert target.id == host
    code = Path(target.code_env(OMEGA)["PYTHONPATH"])
    assert (code / target.host.package / "__init__.py").is_file()
    assert (OMEGA / target.tests).is_dir()
    assert target.holdout is not None
    assert (OMEGA / target.holdout).is_file()
    for path in (*target.lint, *target.typecheck):
        assert (OMEGA / path).is_dir()
    assert set(target.live_services) <= set(target.host.services)


def test_stress_profiles_rate_limit_the_retried_services() -> None:
    for host, service in (("ledger", "fx"), ("scout", "docs")):
        profile = load_target(OMEGA, host).stress()
        assert profile is not None
        assert profile.disclosed
        assert any(r.service == service and r.params.get("status") == 429 for r in profile.rules)
    assert load_target(OMEGA, "triage").stress() is None
