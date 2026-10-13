"""§4.1.2: the crew's own traces. Each role runs as ``kintsugi:<role>``; a step for an incident is
a trace rooted in the ``WORKFLOW`` span ``incident:<INC>`` with session ``inc-<INC>``; every
cost line of the incident is carried on exactly one trace, so the session total equals the
recorded actuals audit C10 reconciles (and the bundle names the session)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext

from kintsugi.control import contracts as c
from kintsugi.control.activities import SPECS, StepContext
from kintsugi.crew import telemetry as t
from kintsugi.crew.casefile import CaseFile
from kintsugi.crew.context import CrewContext
from kintsugi.crew.factory import OmegaCrew
from kintsugi.proof import audit

INC = "INC-0042"


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def _telemetry(state: Path, spans: InMemorySpanExporter) -> t.CrewTelemetry:
    def provider(role: str) -> TracerProvider:
        tp = TracerProvider()
        tp.add_span_processor(SimpleSpanProcessor(spans))
        return tp

    return t.CrewTelemetry(state, tracer_provider=provider)


def _context(span: ReadableSpan) -> SpanContext:
    context = span.get_span_context()
    assert context is not None
    return context


def _step(name: str) -> StepContext:
    return StepContext(name, f"{name}:key", 1, lambda *details: None)


def test_every_activity_has_a_crew_role() -> None:
    assert set(SPECS) == set(t.ROLES)
    roles = set(t.ROLES.values())
    assert roles >= {"sentinel", "pathologist", "archivist", "surgeon", "auditor", "coordinator",
                     "immunologist", "saboteur"}  # §5.7 # fmt: skip
    assert t.workflow_name("surgeon") == "kintsugi:surgeon"
    assert t.session_id(INC) == "inc-INC-0042"


def test_the_incident_of_a_step_comes_from_its_arguments() -> None:
    sig = c.Signature(INC, "ledger", "phi", ("t1",))
    out = c.SurgeonOutcome(INC, "H1", 1, "b", "n", "sha")
    assert t.incident_of("reproduce", [sig]) == INC
    assert t.incident_of("rewind", [[out]]) == INC
    assert t.incident_of("ledger_append", [{"type": "incident", "incident": INC}]) == INC
    assert t.incident_of("escalate", [INC, "no budget"]) == INC
    assert t.incident_of("route_hit", [c.Hit("k", "d", "t", "s"), []]) is None
    assert t.incident_of("anchor_ledger", [7]) is None


def test_a_crew_step_is_a_role_trace_rooted_in_the_incident_session(
    tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    ctx = CrewContext(state=tmp_path / "crew", targets={})
    tel = _telemetry(tmp_path / "telemetry", spans)
    crew = OmegaCrew(ctx, runner=lambda: None, telemetry=tel)  # type: ignore[arg-type,return-value]
    case = CaseFile(ctx.state / "cases", INC)

    def spend(step: StepContext, sig: c.Signature) -> c.Repro:  # a step that spends money
        case.add_cost("pathologist", 0.25, "repro session")
        return c.Repro(True, "sha256:x", 3, 3)

    crew.do_reproduce = spend  # type: ignore[method-assign]
    sig = c.Signature(INC, "ledger", "phi", ("t1",))
    crew.handle(_step("reproduce"), sig)
    crew.handle(_step("ledger_append"), {"type": "transition", "incident": INC})
    finished = {s.name: s for s in spans.get_finished_spans()}
    roots = [s for s in spans.get_finished_spans() if s.name == f"incident:{INC}"]
    assert len(roots) == 2  # one trace per step, each with its incident root
    for root in roots:
        assert root.parent is None
        assert root.attributes is not None
        assert root.attributes["neatlogs.session.id"] == "inc-INC-0042"
        assert root.attributes["openinference.span.kind"] == "WORKFLOW"
    step = finished["pathologist.reproduce"]
    assert step.resource.attributes["neatlogs.workflow_name"] == "kintsugi:pathologist"
    assert step.attributes is not None
    assert step.attributes["neatlogs.agent.cost_usd"] == 0.25
    assert step.parent is not None
    assert step.parent.span_id in {_context(r).span_id for r in roots}
    notary = finished["notary.ledger_append"]
    assert notary.resource.attributes["neatlogs.workflow_name"] == "kintsugi:notary"
    assert notary.attributes is not None
    assert notary.attributes["neatlogs.agent.cost_usd"] == 0.0
    journal = [json.loads(line) for line in tel.journal_path(INC).read_text().splitlines()]
    assert [(j["workflow"], j["session"], j["cost_usd"]) for j in journal] == [
        ("kintsugi:pathologist", "inc-INC-0042", 0.25),
        ("kintsugi:notary", "inc-INC-0042", 0.0),
    ]
    traces = {f"{_context(r).trace_id:032x}" for r in roots}
    assert {j["trace_id"] for j in journal} == traces
    assert all(j["exported"] is False for j in journal)  # no NEATLOGS_API_KEY: local only


def test_a_fleet_step_has_no_incident_root(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    tel = _telemetry(tmp_path, spans)
    with tel.step("route_hit", None):
        pass
    [span] = spans.get_finished_spans()
    assert span.name == "sentinel.route_hit"
    assert span.resource.attributes["neatlogs.workflow_name"] == "kintsugi:sentinel"


def test_each_cost_line_is_reported_once_across_processes_and_threads(
    tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    """Two telemetry instances (the crew and Surgeon pools) share the cursor: concurrent steps
    may split the lines, but the session total equals the case file's actuals exactly."""
    case = CaseFile(tmp_path / "cases", INC)
    crew_pool = _telemetry(tmp_path / "telemetry", spans)
    surgeon_pool = _telemetry(tmp_path / "telemetry", spans)
    errors: list[BaseException] = []

    def work(tel: t.CrewTelemetry, activity: str, n: int) -> None:
        try:
            for i in range(n):
                with tel.step(activity, INC, cost_lines=lambda inc: case.cost_lines()):
                    case.add_cost(t.role_of(activity), 0.01 * (i + 1), f"{activity} {i}")
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=work, args=(crew_pool, "audit_gates", 15)),
               threading.Thread(target=work, args=(surgeon_pool, "surgeon", 15)),
               threading.Thread(target=work, args=(surgeon_pool, "surgeon", 10))]  # fmt: skip
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    reported = sum(float(s.attributes.get("neatlogs.agent.cost_usd", 0.0))  # type: ignore[union-attr]
                   for s in spans.get_finished_spans() if s.name.count(".") == 1)  # fmt: skip
    assert round(reported, 6) == case.spent()
    lines = sum(int(s.attributes.get("kintsugi.cost_lines", 0))  # type: ignore[union-attr]
                for s in spans.get_finished_spans() if s.name.count(".") == 1)  # fmt: skip
    assert lines == len(case.cost_lines()) == 40


def test_the_bundle_names_the_session_c10_reconciles(tmp_path: Path) -> None:
    """The bundle's cost section claims the incident's session; C10 then compares the actuals
    with neatlogs' session cost (here a recorded answer equal to the telemetry total)."""
    from kintsugi.crew import bundler

    case = CaseFile(tmp_path / "cases", INC)
    tel = t.CrewTelemetry(tmp_path / "telemetry")  # export disabled: the SDK's own clients
    for usd in (1.5, 2.25):
        with tel.step("surgeon", INC, cost_lines=lambda inc: case.cost_lines()):
            case.add_cost("surgeon", usd, "session")
    closing: dict[str, Any] = {"incident": INC, "host": "ledger", "state": "ESCALATED",
                               "reason": "budget", "traces": {"failing": []}}  # fmt: skip
    _path, _digest = bundler.emit(tmp_path / "evidence", case, closing,
                                  journal_path=tel.journal_path(INC))  # fmt: skip
    data = json.loads((tmp_path / "evidence" / INC / "bundle.json").read_bytes())
    assert data["cost"]["sessions"] == ["inc-INC-0042"]
    assert data["cost"]["actual_usd"] == data["cost"]["telemetry_usd"] == 3.75
    assert (tmp_path / "evidence" / INC / "telemetry" / "sessions.jsonl").is_file()
    ok = audit.c10_cost(data["cost"]["actual_usd"], {"inc-INC-0042": 3.75})
    assert ok.status is audit.Status.PASS
    off = audit.c10_cost(data["cost"]["actual_usd"], {"inc-INC-0042": 3.0})
    assert off.status is audit.Status.FAIL
