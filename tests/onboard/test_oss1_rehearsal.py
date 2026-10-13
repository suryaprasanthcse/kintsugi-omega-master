"""The OSS-1 onboarding rehearsal (Stage 10, offline, disclosed): exports, guard, O1–O7, CLI."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from kintsugi.cli import main
from kintsugi.onboard import intake, rehearsal, replay_mode, trace_export

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = sorted((REPO / "evidence" / "rehearsals").glob("oss1-*.json"))


def _spans(seed: int = 3) -> list[Any]:
    memory = InMemorySpanExporter()
    provider = TracerProvider(id_generator=trace_export.seeded_ids(seed))
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("test")
    root_attrs = {"openinference.span.kind": "WORKFLOW", "neatlogs.workflow_name": "host:pkg",
                  "neatlogs.tags": "host:pkg,split:train,seed:3", "input.value": '{"id": "T-1"}',
                  "input.mime_type": "application/json"}  # fmt: skip
    with tracer.start_as_current_span("task:T-1", attributes=root_attrs):
        tool_attrs = {"openinference.span.kind": "TOOL", "input.value": '{"domain": "a.test"}'}
        with tracer.start_as_current_span("enrich", attributes=tool_attrs) as s:
            s.record_exception(RuntimeError("enrich a.test: HTTP 429"))
            s.set_status(Status(StatusCode.ERROR, "HTTP 429"))
        with tracer.start_as_current_span("grader.verdict", attributes={
                "openinference.span.kind": "CHAIN",
                "input.value": '{"task_id": "T-1", "verdict": "FAIL:DUPLICATE_CONTACT"}',
                "input.mime_type": "application/json",
                "output.value": '"FAIL:DUPLICATE_CONTACT"',
                "output.mime_type": "application/json"}):  # fmt: skip
            pass
    return list(memory.get_finished_spans())


def test_captured_spans_export_in_the_public_api_shape_and_replay(tmp_path: Path) -> None:
    exports = trace_export.to_exports(_spans())
    assert len(exports) == 1
    trace_id, files = next(iter(exports.items()))
    assert sorted(files) == ["raw_spans.json", "spans.json", "trace.json"]
    summary = files["trace.json"]["body"]["data"]
    assert summary["traceName"] == "task:T-1"
    assert summary["workflowName"] == "host:pkg"
    assert summary["tags"] == ["host:pkg", "split:train", "seed:3"]
    assert summary["toolCallsCount"] == 1 and summary["spansCount"] == 3  # noqa: PT018
    trace_export.write_exports(exports, tmp_path)
    [record] = replay_mode.ExportedTraces(tmp_path).records()  # the live load_trace path
    assert record.trace_id == trace_id
    assert record.task_id == "T-1"
    assert [v.verdict for v in record.verdicts] == ["FAIL:DUPLICATE_CONTACT"]
    tool = next(s for s in record.spans if s.name == "enrich")
    assert tool.failed and "HTTP 429" in (tool.error or "")  # noqa: PT018
    repro = record.repro()  # the tags name the task to re-run, as a live trace's would
    assert repro is not None
    assert (repro.host, repro.task_id, repro.split, repro.seed) == ("pkg", "T-1", "train", 3)


def test_model_spans_carry_tokens_and_traces_carry_metered_costs(tmp_path: Path) -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider(id_generator=trace_export.seeded_ids(9))
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("test")
    root = {"openinference.span.kind": "WORKFLOW", "neatlogs.workflow_name": "host:pkg"}
    model = {"openinference.span.kind": "LLM", "llm.model_name": "claude-haiku-5-5",
             "llm.provider": "anthropic", "llm.token_count.prompt": 554,
             "llm.token_count.completion": 184}  # fmt: skip
    with (tracer.start_as_current_span("task:T-9", attributes=root),
          tracer.start_as_current_span("claude-haiku-5-5", attributes=model)):  # fmt: skip
        pass
    exports = trace_export.to_exports(memory.get_finished_spans())
    [files] = exports.values()
    summary = files["trace.json"]["body"]["data"]
    assert (summary["totalTokens"], summary["llmCallsCount"]) == (738, 1)
    llm = next(s for s in files["spans.json"]["body"]["data"]["spans"] if s["spanType"] == "LLM")
    assert (llm["promptTokens"], llm["completionTokens"], llm["totalTokens"]) == (554, 184, 738)
    assert (llm["model"], llm["provider"]) == ("claude-haiku-5-5", "anthropic")
    costs = trace_export.task_costs(
        [{"task": "T-9", "service": "geo"}, {"task": "T-9", "service": "crm"},
         {"task": "T-1", "service": "geo"}], {"geo": 0.0001, "crm": 0.0003},
        [{"task": "T-9", "cost_usd": 0.0021}])  # fmt: skip
    assert costs == {"T-9": pytest.approx(0.0025), "T-1": pytest.approx(0.0001)}
    assert trace_export.apply_costs(exports, costs) == 1
    assert summary["totalCostUsd"] == pytest.approx(0.0025)
    trace_export.write_exports(exports, tmp_path)
    [record] = replay_mode.ExportedTraces(tmp_path).records()
    assert record.cost_usd == pytest.approx(0.0025)
    assert record.total_tokens == 738


def test_seeded_ids_export_the_same_ids_for_the_same_seed() -> None:
    assert list(trace_export.to_exports(_spans(5))) == list(trace_export.to_exports(_spans(5)))
    assert list(trace_export.to_exports(_spans(5))) != list(trace_export.to_exports(_spans(6)))


def test_previews_are_truncated_and_say_so() -> None:
    long = "x" * (trace_export.PREVIEW_LIMIT + 10)
    preview = trace_export._preview(long, "text/plain")
    assert preview["truncated"] is True
    assert len(preview["preview"]) == trace_export.PREVIEW_LIMIT


def test_the_guard_refuses_non_loopback_connections_and_counts_them() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with trace_export.LoopbackGuard() as guard:
        client = socket.create_connection(server.getsockname(), timeout=5)  # loopback: allowed
        client.close()
        outside = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(trace_export.NonLoopbackRefused):
            outside.connect(("192.0.2.1", 443))  # TEST-NET-1: never reached
        outside.close()
    server.close()
    assert guard.refused == ["('192.0.2.1', 443)"]
    assert socket.socket.connect is not None


def test_the_intake_fixture_has_every_consent_and_is_foreign() -> None:
    accepted = intake.parse(rehearsal.INTAKE.read_text(encoding="utf-8"))
    assert accepted.consents == intake.CONSENTS
    assert accepted.foreign and accepted.pr_only  # noqa: PT018
    assert accepted.entry_point == "practice_crm/agent.py"
    assert (rehearsal.PRACTICE / accepted.entry_point).is_file()


def test_the_checkout_digest_ignores_tool_caches(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = rehearsal._tree_sha(tmp_path)
    for cache in (".ruff_cache", ".pytest_cache", "pkg/__pycache__"):
        (tmp_path / cache).mkdir(parents=True)
        (tmp_path / cache / "junk").write_text("cache", encoding="utf-8")
    assert rehearsal._tree_sha(tmp_path) == before
    (tmp_path / "pkg" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert rehearsal._tree_sha(tmp_path) != before


@pytest.fixture(scope="module")
def summary(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("oss1")
    cfg = rehearsal.OnboardRehearsal("oss1-test", root / "run", seed=1, limit=8,
                                     log=lambda line: None)  # fmt: skip
    return rehearsal.run(cfg)


def test_the_rehearsal_onboards_o1_to_o7_offline(summary: dict[str, Any]) -> None:
    exports, record = summary["exports"], summary["onboarding"]
    assert exports["tasks"] == exports["traces"] == 8
    assert sum(exports["verdicts"].values()) == 8
    assert exports["model_mode"] == "offline"
    assert exports["priced_traces"] == 8  # every trace carries its metered cost
    assert exports["metered_cost_usd"] > 0
    assert record["O1_intake"]["consents"] == 5
    assert record["O1_intake"]["foreign"] is True
    assert record["O6_baseline"]["replay"]["traces"] == 8
    assert record["O6_baseline"]["cost_usd"] == pytest.approx(exports["metered_cost_usd"])
    failed = sum(n for verdict, n in exports["verdicts"].items() if verdict != "PASS")
    assert record["O6_baseline"]["passed"] == 8 - failed
    assert record["O7_immune_scan"]["receipt"] == {
        "pr_only": True, "merge": False,
        "foreign_posts": "each one waits for the Architect's approval"}  # fmt: skip
    scan = record["O7_immune_scan"]
    assert scan["status"] in {"scanned", "skipped"}  # skipped where entire-graph is missing
    if scan["status"] == "scanned":
        assert {m["antibody"] for m in scan["matches"]} == {"AB-1@1.1"}


def test_nothing_leaves_the_machine(summary: dict[str, Any]) -> None:
    network = summary["network"]
    assert network["non_loopback_connections_refused"] == 0
    assert network["foreign_posts"] == 0
    assert summary["disclosed"]


def test_the_same_seed_exports_the_same_traces(summary: dict[str, Any], tmp_path: Path) -> None:
    again = rehearsal.run(rehearsal.OnboardRehearsal("oss1-again", tmp_path / "run", seed=1,
                                                     limit=8, log=lambda line: None))  # fmt: skip
    assert again["exports"]["verdicts"] == summary["exports"]["verdicts"]
    assert [e["source_trace"] for e in again["onboarding"]["O5_evals"]["seeds"]] == [
        e["source_trace"] for e in summary["onboarding"]["O5_evals"]["seeds"]]  # fmt: skip
    assert again["exports"]["metered_cost_usd"] == summary["exports"]["metered_cost_usd"]


def _receipt_and_replay(onboarding: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """The receipt and the replayed trace count, in either record format: the A-74 run's
    (O3_replay, O7_receipt) or the desk's §5.9.4 steps (O6_baseline, O7_immune_scan)."""
    if "O7_immune_scan" in onboarding:
        return onboarding["O7_immune_scan"]["receipt"], onboarding["O6_baseline"]["replay"][
            "traces"]  # fmt: skip
    return onboarding["O7_receipt"], onboarding["O3_replay"]["traces"]


@pytest.mark.skipif(not EVIDENCE, reason="no committed OSS-1 rehearsal evidence")
@pytest.mark.parametrize("path", EVIDENCE, ids=[p.stem for p in EVIDENCE])
def test_every_committed_evidence_is_an_offline_pr_only_onboarding(path: Path) -> None:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    assert evidence["kind"] == "onboarding-rehearsal"
    assert evidence["phase"] == "rehearsal"
    assert evidence["network"]["non_loopback_connections_refused"] == 0
    assert evidence["network"]["foreign_posts"] == 0
    assert evidence["onboarding"]["O1_intake"]["consents"] == 5
    receipt, replayed = _receipt_and_replay(evidence["onboarding"])
    assert receipt["pr_only"] is True
    assert receipt["merge"] is False
    assert evidence["exports"]["traces"] == replayed


@pytest.mark.skipif(not EVIDENCE, reason="no committed OSS-1 rehearsal evidence")
def test_the_newest_evidence_matches_the_committed_fixtures() -> None:
    evidence = json.loads(EVIDENCE[-1].read_text(encoding="utf-8"))
    assert evidence["intake"]["sha256"] == rehearsal._sha(rehearsal.INTAKE)
    assert evidence["checkout"]["sha256"] == rehearsal._tree_sha(rehearsal.PRACTICE)


def test_the_cli_runs_the_rehearsal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "oss1.json"
    assert main(["onboard", "rehearse", "--root", str(tmp_path), "--run", "oss1-cli",
                 "--limit", "3", "--out", str(out)]) == 0  # fmt: skip
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["run"] == "oss1-cli"
    assert written["exports"]["traces"] == 3
    assert not (tmp_path / "oss1-cli").exists()  # the scratch checkout is removed
    capsys.readouterr()
