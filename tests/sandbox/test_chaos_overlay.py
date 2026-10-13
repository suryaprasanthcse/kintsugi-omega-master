"""Environment sabotage through the chaos proxy (§7.2, §7.7): compile, activate, fire, expire."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kintsugi.crew.saboteur import compose
from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.sandbox.chaos_overlay import ChaosOverlay, OverlayError, compile_env
from kintsugi.sandbox.httpserve import HttpRequest, HttpResponse

REPO = Path(__file__).resolve().parents[2]
EXTERNAL = {"ledger": {"fx_rate": "fx"}, "scout": {"fetch_document": "docs"}}
NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


def _spec(cls: str, host: str = "ledger", **params: Any) -> dict[str, Any]:
    return compose("S-A0007", cls, host=host, framing="Arena request",
                   ground_truth={"files": [], "mechanism": cls},
                   not_before=NOW.isoformat(), seed=1, params=params,
                   duration_s=600.0)  # fmt: skip


def test_every_environment_class_compiles_to_a_rule_on_the_tools_service() -> None:
    cases = {
        "env.rate_limit": ({"tool": "fx_rate", "rate": 0.2, "burst": 3}, "status"),
        "env.latency": ({"tool": "fx_rate", "p50_ms": 200, "p95_ms": 900}, "latency"),
        "env.schema_drift": ({"tool": "fx_rate", "change": "drop_field"}, "schema_drift"),
        "env.partial_response": ({"tool": "fx_rate", "truncation_pct": 40}, "partial"),
        "env.pagination_change": ({"tool": "fx_rate", "page_size": 2, "cursor_format": "offset"},
                                  "pagination"),
    }  # fmt: skip
    for cls, (params, fault) in cases.items():
        [rule] = compile_env(_spec(cls, **params), EXTERNAL)
        assert (rule.service, rule.fault) == ("fx", fault), cls
        assert rule.params["sabotage"] == "S-A0007"
    [partial] = compile_env(_spec("env.partial_response", tool="fx_rate", truncation_pct=40),
                            EXTERNAL)  # fmt: skip
    assert partial.params["keep"] == pytest.approx(0.6)
    with pytest.raises(OverlayError, match="external tool"):
        compile_env(_spec("env.rate_limit", tool="post_journal_entry", rate=0.2, burst=1),
                    EXTERNAL)  # fmt: skip
    with pytest.raises(OverlayError, match="patch"):
        compile_env({**_spec("env.rate_limit", tool="fx_rate", rate=0.2, burst=1),
                     "class": "code.regression.exception_swallow"}, EXTERNAL)  # fmt: skip


def test_an_overlay_is_active_for_its_duration_and_survives_bad_files(tmp_path: Path) -> None:
    overlay = ChaosOverlay(tmp_path / "active", EXTERNAL)
    path = overlay.deliver(_spec("env.rate_limit", tool="fx_rate", rate=0.5, burst=2), now=NOW)
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    assert body["sabotage"] == "S-A0007"
    assert body["until"] == (NOW + timedelta(seconds=600)).isoformat()
    (tmp_path / "active" / "broken.json").write_text("{not json", encoding="utf-8")
    assert overlay.active(NOW + timedelta(seconds=1)) == ["S-A0007"]
    assert overlay.active(NOW - timedelta(seconds=1)) == []
    assert overlay.active(NOW + timedelta(seconds=601)) == []
    [(key, rule)] = overlay.rules(NOW + timedelta(seconds=5))
    assert key == "S-A0007:0"
    assert rule.fault == "status"


def test_the_fleet_overlay_knows_every_hosts_external_tools(tmp_path: Path) -> None:
    overlay = ChaosOverlay.for_fleet(tmp_path, REPO)
    assert overlay.folder == tmp_path / "chaos" / "active"
    assert overlay.external["ledger"] == {"fx_rate": "fx"}
    assert set(overlay.external["triage"]) == {"apply_label", "comment"}


def test_overlay_draws_never_move_the_background_profiles_faults() -> None:
    profile = chaos.background_profile().with_seed(11)
    extra = compile_env(_spec("env.rate_limit", tool="fx_rate", rate=0.3, burst=1), EXTERNAL)
    keyed = [("S-A0007:0", extra[0])]
    for occurrence in range(200):
        plain = chaos.decide(profile, "fx", "/rate", "t1", "k", occurrence)
        mixed = chaos.decide(profile, "fx", "/rate", "t1", "k", occurrence, keyed)
        assert [d for d in mixed if isinstance(d.rule, int)] == plain


def test_a_burst_fails_a_run_of_calls_once_it_fires() -> None:
    rule = chaos.Rule("fx", "status", 0.05, "*", {"status": 429, "burst": 4})
    profile = chaos.Profile("empty", 3, ())
    fired = [bool(chaos.decide(profile, "fx", "/rate", "t", "k", o, [("S:0", rule)]))
             for o in range(400)]  # fmt: skip
    single = chaos.Rule("fx", "status", 0.05, "*", {"status": 429})
    starts = [
        o
        for o in range(400)
        if chaos.decide(profile, "fx", "/rate", "t", "k", o, [("S:0", single)])
    ]
    assert starts, "the draw should fire at least once in 400 calls"
    for start in starts:
        assert all(fired[start : start + 4]), start  # the burst follows every firing call
    assert sum(fired) >= len(starts)


def test_latency_is_log_normal_through_p50_and_p95() -> None:
    def delay(draw: float) -> float:
        return chaos.latency_s(chaos.Decision("S:0", "latency", {"p50_ms": 200, "p95_ms": 900},
                                              draw))  # fmt: skip

    assert delay(0.5) == pytest.approx(0.2, rel=1e-6)
    assert delay(0.95) == pytest.approx(0.9, rel=1e-6)
    assert delay(0.05) < 0.2 < delay(0.7) < 0.9
    uniform = chaos.Decision(0, "latency", {"latency_ms": [10, 30]}, 0.5)
    assert chaos.latency_s(uniform) == pytest.approx(0.02)


def _response(body: Any) -> HttpResponse:
    return HttpResponse.of_json(200, body)


def _shaped(body: Any, fault: str, **params: Any) -> Any:
    decision = chaos.Decision("S:0", fault, params, 0.5)  # type: ignore[arg-type]
    return json.loads(chaos.apply_body_faults(_response(body), [decision]).body)


def test_body_faults_drift_truncate_and_paginate() -> None:
    rows = [{"amount": 1, "rate": 1.1}, {"amount": 2, "rate": 1.2}, {"amount": 3, "rate": 1.3}]
    assert _shaped(rows, "schema_drift", change="rename_field")[0] == {"amount_v2": 1, "rate": 1.1}
    assert _shaped(rows, "schema_drift", change="drop_field")[0] == {"rate": 1.1}
    assert _shaped(rows, "schema_drift", change="type_change")[0] == {"amount": "1", "rate": 1.1}
    assert _shaped(rows, "partial", keep=0.34) == rows[:1]
    assert _shaped(rows, "pagination", page_size=2, cursor_format="offset") == {
        "items": rows[:2], "next_cursor": 2}  # fmt: skip
    wrapped = _shaped({"results": rows, "total": 3}, "pagination", page_size=1,
                      cursor_format="opaque")  # fmt: skip
    assert wrapped["results"] == rows[:1]
    assert isinstance(wrapped["next_cursor"], str)


def test_the_proxy_fires_an_active_overlay_and_logs_the_sabotage(tmp_path: Path) -> None:
    overlay = ChaosOverlay(tmp_path / "active", EXTERNAL)
    proxy = chaos.ChaosProxy({"fx": lambda request: HttpResponse.of_json(200, {"rate": 1.0})},
                             chaos.Profile("empty", 1, ()), overlay=overlay,
                             sleep=lambda s: None)  # fmt: skip

    def call() -> HttpResponse:
        return proxy(HttpRequest("GET", "/fx/rate", {"pair": "EURUSD"}, {"x-kintsugi-task": "t1"},
                                 b""))  # fmt: skip

    assert call().status == 200  # nothing delivered yet
    overlay.deliver(_spec("env.rate_limit", tool="fx_rate", rate=0.5, burst=1))
    statuses = [call().status for _ in range(40)]
    assert 429 in statuses and 200 in statuses  # noqa: PT018
    faulted = [row for row in proxy.log if row["status"] == 429]
    assert faulted and all(row["sabotage"] == "S-A0007" for row in faulted)  # noqa: PT018
    proxy.close()
