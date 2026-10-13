"""Onboarding O1–O7 (Stage 10), offline: intake, health card, replay mode, evals, scan, terms."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from kintsugi.memory import graph
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.onboard import (
    desk,
    eval_synth,
    immune_scan,
    instrument,
    intake,
    pipeline,
    replay_mode,
)

REPO = Path(__file__).resolve().parents[2]
NEATLOGS = REPO / "tests" / "fixtures" / "neatlogs"
CRM = REPO / "tests" / "fixtures" / "rehearsal" / "crm_practice"
TRACE = "d23ffdad100e3a3bd55365ebc266447d"


def _form(repo: str = "octo-org/support-agent", consents: tuple[str, ...] = intake.CONSENTS,
          entry: str = "agent/main.py") -> str:  # fmt: skip
    boxes = "\n".join(f"- [x] {c}" for c in consents)
    return (f"### Repository\n\n{repo}\n\n### Agent entry point\n\n{entry}\n\n"
            "### What goes wrong\n\nDuplicate tickets after a timeout.\n\n"
            f"### Consent\n\n{boxes}\n")  # fmt: skip


def test_the_issue_form_and_the_parser_agree() -> None:
    form = yaml.safe_load((REPO / ".github" / "ISSUE_TEMPLATE" / "clinic-intake.yml").read_text(
        encoding="utf-8"))  # fmt: skip
    labels = {item["attributes"]["label"] for item in form["body"] if item["type"] != "markdown"}
    assert labels == {"Repository", "Agent entry point", "Run command", "Model provider",
                      "What goes wrong", "Consent"}  # fmt: skip
    optional = {i["attributes"]["label"] for i in form["body"]
                if i["type"] not in ("markdown", "checkboxes")
                and not i.get("validations", {}).get("required")}  # fmt: skip
    assert optional == {"Run command", "Model provider"}  # each consent box is required below
    consent = next(i for i in form["body"] if i.get("id") == "consent")
    assert tuple(o["label"] for o in consent["attributes"]["options"]) == intake.CONSENTS
    assert all(o["required"] for o in consent["attributes"]["options"])


def test_intake_requires_every_consent_and_marks_foreign_repos_pr_only() -> None:
    accepted = intake.parse(_form())
    assert accepted.repo == "octo-org/support-agent"
    assert accepted.run_command is None and accepted.model_provider is None  # noqa: PT018
    extra = _form().replace(
        "### What goes wrong",
        "### Run command\n\n`python -m agent`\n\n"
        "### Model provider\n\n_No response_\n\n### What goes wrong",
    )
    assert intake.parse(extra).run_command == "python -m agent"
    assert intake.parse(extra).model_provider is None  # GitHub's empty optional answer
    assert accepted.foreign
    assert accepted.pr_only
    assert pipeline.ship_config(accepted).pr_only  # O7: Omega never merges there
    assert not intake.parse(_form(repo=intake.OWN[0])).pr_only
    assert intake.parse(_form(repo="https://github.com/octo-org/support-agent/")).repo == (
        "octo-org/support-agent")  # fmt: skip
    with pytest.raises(intake.IntakeRefused, match="consent"):
        intake.parse(_form(consents=intake.CONSENTS[:-1]))
    unchecked = _form().replace("- [x] Fixes arrive", "- [ ] Fixes arrive")
    with pytest.raises(intake.IntakeRefused, match="consent"):
        intake.parse(unchecked)
    for bad in ("octo-org", "octo/org/x", "a b/c", "../etc"):
        with pytest.raises(intake.IntakeRefused, match="owner/name"):
            intake.parse(_form(repo=bad))
    for bad in ("/etc/passwd", "../outside.py", "agent/../../x.py", ""):
        with pytest.raises(intake.IntakeRefused, match="entry point"):
            intake.parse(_form(entry=bad))


def test_the_health_card_reads_a_checkout_statically(tmp_path: Path) -> None:
    card = instrument.scan(CRM)
    assert card.python_files > 0
    assert any("retry" in r for r in card.retry_wrappers)
    assert card.tests
    agent = tmp_path / "agent"
    (agent / "pkg").mkdir(parents=True)
    (agent / "pkg" / "main.py").write_text(
        "import anthropic\nimport httpx\n\n"
        "def tool(f):\n    return f\n\n"
        "@tool\ndef create_ticket(x):\n"
        "    return httpx.post('https://example.invalid', json=x)\n\n"
        "def run(c):\n    return c.messages.create(model='m', messages=[])\n",
        encoding="utf-8")  # fmt: skip
    card = instrument.scan(agent)
    assert card.model_imports == ["anthropic"]
    assert card.model_calls == 1
    assert card.tools == ["pkg/main.py:create_ticket"]
    assert card.side_effecting_http == ["pkg/main.py:9 httpx.post"]
    assert not card.neatlogs_init and not card.tests  # noqa: PT018
    assert any("neatlogs" in n for n in card.needs)


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    folder = tmp_path / "exports" / TRACE
    folder.mkdir(parents=True)
    for name, fixture in (("trace.json", "trace_get"), ("spans.json", "trace_spans"),
                          ("raw_spans.json", "trace_raw_spans")):  # fmt: skip
        shutil.copyfile(NEATLOGS / f"{fixture}.json", folder / name)
    return tmp_path / "exports"


def test_replay_mode_reads_exports_like_live_traces(exports: Path) -> None:
    from kintsugi.senses.records import load_trace
    from tests.senses.test_records_and_tags import FixtureApi

    replayed = replay_mode.ExportedTraces(exports).records()
    live = load_trace(FixtureApi(), TRACE)
    assert [r.trace_id for r in replayed] == [live.trace_id]
    assert replayed[0].spans == live.spans  # the same TraceRecord as the live path
    # data-only exports (no response envelope) read the same
    folder = exports / TRACE
    for name in ("trace.json", "spans.json", "raw_spans.json"):
        raw = json.loads((folder / name).read_text(encoding="utf-8"))
        (folder / name).write_text(json.dumps(raw["body"]["data"]), encoding="utf-8")
    assert replay_mode.ExportedTraces(exports).records()[0].spans == live.spans
    (folder / "spans.json").unlink()
    with pytest.raises(replay_mode.ExportError, match="missing"):
        replay_mode.ExportedTraces(exports).records()


def test_evals_baseline_and_the_whole_pipeline(exports: Path, monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:  # fmt: skip
    records = replay_mode.ExportedTraces(exports).records()
    base = eval_synth.baseline(records)
    assert base["traces"] == 1
    assert 0.0 <= base["wilson_95"][0] <= base["pass_rate"] <= base["wilson_95"][1] <= 1.0
    assert eval_synth.baseline([]) == {"traces": 0, "passed": 0, "pass_rate": 0.0,
                                       "wilson_95": [0.0, 0.0], "cost_usd": 0.0,
                                       "p95_duration_ms": 0.0}  # fmt: skip
    assert base["p95_duration_ms"] == records[0].duration_ms
    seeds = eval_synth.synthesize(records)
    assert all(s["source_trace"] == TRACE for s in seeds)

    def unavailable(*args: object, **kwargs: object) -> object:
        raise graph.GraphError("entire-graph is not installed here")

    monkeypatch.setattr(graph, "snapshot", unavailable)
    library = AntibodyLibrary(REPO / "antibodies")
    assert immune_scan.scan(CRM, library, package_dir="practice_crm")["status"] == "skipped"
    record = pipeline.onboard(_form(entry="practice_crm/agent.py"), CRM, exports, library,
                              package_dir="practice_crm", root=tmp_path / "cases")  # fmt: skip
    assert [k for k in record if k != "host"] == [
        "O1_intake", "O2_sandbox", "O3_instrument", "O4_entire", "O5_evals", "O6_baseline",
        "O7_immune_scan"]  # fmt: skip
    assert all(record[k]["ok"] for k in record if k != "host")
    assert record["O1_intake"]["foreign"] is True
    sandbox = Path(record["O2_sandbox"]["sandbox"])
    assert sandbox.is_relative_to(tmp_path / "cases")  # never the maintainers' checkout
    assert record["O2_sandbox"]["manifests"] == ["pyproject.toml"]
    assert record["O2_sandbox"]["install"]["status"] == "not run"
    assert record["O4_entire"]["status"] == "skipped"  # entire-graph unavailable here
    assert record["O5_evals"]["kinds"]["existing-test"] >= 1
    assert record["O5_evals"]["holdout_manifest"].startswith("sha256:")
    assert record["O6_baseline"]["replay"]["traces"] == 1
    receipt = record["O7_immune_scan"]["receipt"]
    assert receipt["pr_only"] is True
    assert receipt["merge"] is False
    assert "Architect's approval" in receipt["foreign_posts"]
    assert not list(CRM.glob("*.onboard*"))  # the checkout itself is never written
    with pytest.raises(intake.IntakeRefused):
        pipeline.onboard(_form(consents=()), CRM, exports, library)
    missing = pipeline.onboard(_form(entry="no/such.py"), CRM, exports, library,
                               root=tmp_path / "missing")  # fmt: skip
    assert missing["O2_sandbox"]["ok"] is False
    assert "O3_instrument" not in missing  # a failed step ends the onboarding


def test_the_instrumentation_patch_goes_before_the_first_import(tmp_path: Path) -> None:
    source = '"""An agent."""\nfrom __future__ import annotations\n\nimport anthropic\n'
    patched = desk.instrumentation_patch(source, "host:x")
    assert patched.index("neatlogs.init(") < patched.index("import anthropic")
    assert patched.index("from __future__") < patched.index("import neatlogs")
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "main.py").write_text(source, encoding="utf-8")
    d = desk.OnboardingDesk(tmp_path / "cases", AntibodyLibrary(REPO / "antibodies"))
    d.register(desk.OnboardingCase("x", _form(entry="main.py"), str(agent)))
    for step in ("O1", "O2", "O3"):
        assert d.step("x", step).ok, step
    o3 = d.result("x", "O3")
    assert o3["patched"] is True
    assert o3["health_card_after"]["neatlogs_init"] is True
    assert "neatlogs" not in (agent / "main.py").read_text(encoding="utf-8")  # original intact
    assert d.step("x", "O9").ok is False
    assert d.step("unregistered", "O1").detail["error"].startswith("no onboarding case")


def test_the_eval_split_is_deterministic_and_the_manifest_pins_the_holdout() -> None:
    cases = [{"id": f"case-{i}"} for i in range(200)]
    train, holdout = desk.split(cases)
    assert desk.split(cases) == (train, holdout)
    assert 40 <= len(holdout) <= 80
    assert len(train) + len(holdout) == 200
    assert desk.manifest_sha(holdout) == desk.manifest_sha(list(reversed(holdout)))
    assert desk.manifest_sha(holdout) != desk.manifest_sha(holdout[1:])
