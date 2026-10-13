"""Stage 10 through the control plane: the Onboarding workflow, the scanner, transient retries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from kintsugi.cli import main
from kintsugi.control.activities import StepContext
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.onboard import eval_synth, instrument, replay_mode, trace_export
from kintsugi.onboard.desk import STEPS, OnboardingCase, OnboardingDesk
from kintsugi.onboard.workflow import DeskCrew, record, run_onboarding

REPO = Path(__file__).resolve().parents[2]
CRM = REPO / "tests" / "fixtures" / "rehearsal" / "crm_practice"
INTAKE = REPO / "tests" / "fixtures" / "rehearsal" / "oss1" / "intake.md"


# ---- the scanner -----------------------------------------------------------------------------


def test_the_scanner_finds_span_declared_tools_and_wrapped_http() -> None:
    card = instrument.scan(CRM)
    assert card.tools == [
        "practice_crm/tools.py:enrich",
        "practice_crm/tools.py:create_contact",
        "practice_crm/tools.py:list_contacts",
    ]  # @span(kind="TOOL") each
    assert card.side_effecting_tools == ["practice_crm/tools.py:create_contact"]
    methods = {call.split(" ")[1] for call in card.http_calls}
    assert {"GET", "POST"} <= methods  # external.call("geo", "GET", …), ("crm", "POST", …)
    assert any("external.call" in call for call in card.side_effecting_http)
    assert not any("tool declarations" in need for need in card.needs)


def test_the_scanner_reads_client_methods_and_kinds(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text(
        "import httpx\nfrom neatlogs import span\n\n"
        '@span(kind="TOOL", name="open_ticket")\n'
        "def create(client, x):\n    return client.request(\"PUT\", '/t', json=x)\n\n"
        '@span(kind="CHAIN")\n'
        "def plan(session):\n    return session.get('/plan')\n\n"
        "def cleanup(api):\n    api.delete('/t/1')\n\n"
        "def lookup(d):\n    return d.get('key')\n",
        encoding="utf-8")  # fmt: skip
    card = instrument.scan(tmp_path)
    assert card.tools == ["agent.py:create (open_ticket)"]  # CHAIN spans are not tools
    assert card.side_effecting_tools == ["agent.py:create"]
    assert sorted(c.split(" ")[1] for c in card.http_calls) == ["DELETE", "GET", "PUT"]
    assert sorted(c.split(" ")[1] for c in card.side_effecting_http) == [
        "api.delete", "client.request"]  # fmt: skip


# ---- eval synthesis: transient retries are not seeds -----------------------------------------


def _trace(tracer: Any, task: str, verdict: str | None, tool_error: bool,
           root_error: bool = False) -> None:  # fmt: skip
    with tracer.start_as_current_span(f"task:{task}", attributes={
            "openinference.span.kind": "WORKFLOW"}) as root:  # fmt: skip
        with tracer.start_as_current_span("enrich", attributes={
                "openinference.span.kind": "TOOL"}) as tool:  # fmt: skip
            if tool_error:
                tool.record_exception(RuntimeError("HTTP 429"))
                tool.set_status(Status(StatusCode.ERROR, "HTTP 429"))
        if verdict is not None:
            with tracer.start_as_current_span("grader.verdict", attributes={
                    "openinference.span.kind": "CHAIN",
                    "input.value": json.dumps({"task_id": task, "verdict": verdict}),
                    "input.mime_type": "application/json",
                    "output.value": json.dumps(verdict),
                    "output.mime_type": "application/json"}):  # fmt: skip
                pass
        if root_error:
            root.set_status(Status(StatusCode.ERROR, "crashed"))


def test_transient_retries_are_listed_but_never_seeded(tmp_path: Path) -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider(id_generator=trace_export.seeded_ids(4))
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("test")
    _trace(tracer, "T-retry", "PASS", tool_error=True)  # a 429 the agent retried through
    _trace(tracer, "T-fail", "FAIL:DUPLICATE_CONTACT", tool_error=True)
    _trace(tracer, "T-crash", None, tool_error=False, root_error=True)  # no verdict, task failed
    _trace(tracer, "T-ok", "PASS", tool_error=False)
    trace_export.write_exports(trace_export.to_exports(memory.get_finished_spans()), tmp_path)
    records = replay_mode.ExportedTraces(tmp_path).records()
    seeded = {s["source_trace"] for s in eval_synth.synthesize(records)}
    by_task = {r.task_id: r.trace_id for r in records}
    assert seeded == {by_task["T-fail"], by_task["T-crash"]}
    [retry] = eval_synth.transient(records)
    assert retry["task_id"] == "T-retry"
    assert retry["recovered_spans"] == ["enrich"]
    assert eval_synth.baseline(records)["passed"] == 2  # the retry passed; the crash did not


# ---- the Onboarding workflow -----------------------------------------------------------------


def _desk(tmp_path: Path) -> tuple[OnboardingDesk, str]:
    desk = OnboardingDesk(tmp_path / "cases", AntibodyLibrary(REPO / "antibodies"))
    form = INTAKE.read_text(encoding="utf-8")
    desk.register(OnboardingCase("crm-practice", form, str(CRM), None, "practice_crm",
                                 ("create_contact",)))  # fmt: skip
    return desk, "crm-practice"


def test_the_onboarding_workflow_runs_o1_to_o7_as_activities(tmp_path: Path) -> None:
    desk, host = _desk(tmp_path)
    results = run_onboarding(desk, host)  # an in-process Temporal test server
    assert [r.name for r in results] == list(STEPS)
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    rec = record(results)
    assert rec["O1_intake"]["pr_only"] is True
    assert rec["O3_instrument"]["health_card"]["side_effecting_tools"] == [
        "practice_crm/tools.py:create_contact"]  # fmt: skip
    assert rec["O7_immune_scan"]["receipt"]["merge"] is False
    for step in STEPS:  # each activity left its result in the case folder
        assert (desk.folder(host) / f"{step}.json").is_file()


def test_the_onboarding_crew_serves_only_onboard_step(tmp_path: Path) -> None:
    desk, host = _desk(tmp_path)
    crew = DeskCrew(desk)
    ctx = StepContext("onboard_step", "k", 1, lambda *a: None)
    assert crew.handle(ctx, host, "O1").ok
    with pytest.raises(NotImplementedError, match="seal_sabotage"):
        crew.handle(StepContext("seal_sabotage", "k", 1, lambda *a: None), "S-1")


def test_kintsugi_onboard_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["onboard", "run", "--form", str(INTAKE), "--checkout", str(CRM),
                 "--package-dir", "practice_crm", "--side-effect-tool", "create_contact",
                 "--root", str(tmp_path / "cases")])  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["host"] == "crm-practice"
    assert out["O7_immune_scan"]["receipt"]["pr_only"] is True
    refused = tmp_path / "refused.md"
    refused.write_text(INTAKE.read_text(encoding="utf-8").replace("- [x] Fixes", "- [ ] Fixes"),
                       encoding="utf-8")  # fmt: skip
    assert main(["onboard", "run", "--form", str(refused), "--checkout", str(CRM)]) == 1
