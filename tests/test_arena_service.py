"""The Arena service and board (Stage 11): the static board, the real factory, the CLI."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kintsugi import arena_service as svc
from kintsugi.arena_api import NOT_BEFORE_S, PARAMS
from kintsugi.cli import main
from kintsugi.crew.runtime import RuntimeConfig, SabotageConfig
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.proof.sealed import new_identity

REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / "arena"
GOOD = {"host": "ledger", "class": "env.rate_limit",
        "params": {"tool": "fx_rate", "rate": 0.2, "burst": 3}}  # fmt: skip


def _cfg(tmp_path: Path, *, sabotage: bool) -> svc.ArenaConfig:
    sab = None
    if sabotage:
        _secret, recipient = new_identity()
        sab = SabotageConfig(tmp_path / "store", tmp_path / "unused.agekey", recipient)
    return svc.ArenaConfig(repo=REPO, sabotage=sab, state=tmp_path / "arena",
                           window=tmp_path / "no-window.json", daily_cap=5)  # fmt: skip


# ---- the static board -----------------------------------------------------------------------


def test_the_board_is_static_and_makes_no_external_requests() -> None:
    files = {p.name: p.read_text(encoding="utf-8") for p in BOARD.iterdir() if p.is_file()}
    assert sorted(files) == ["board.css", "board.js", "index.html"]
    for name, text in files.items():
        urls = set(re.findall(r"https?://[^\s\"')]+", text))
        assert urls <= {"http://www.w3.org/2000/svg"}, (name, urls)  # an XML namespace only
        assert "@import" not in text, name
        assert "cdn" not in text.lower(), name
    js = files["board.js"]
    assert set(re.findall(r'"(/api/[a-z]+)"', js)) == {"/api/state", "/api/sabotage"}
    assert "innerHTML" not in js  # API values are inserted as text only
    assert "eval(" not in js
    html = files["index.html"]
    assert re.findall(r'(?:src|href)="([^"]+)"', html) == ["board.css", "board.js"]
    for section in ("fleet", "incidents", "immunity", "curve", "ledger", "escalations",
                    "sabotage"):  # fmt: skip
        assert f'id="{section}"' in html, section


def test_the_app_serves_the_board_beside_the_api(tmp_path: Path) -> None:
    client = TestClient(svc.build_app(_cfg(tmp_path, sabotage=False)))
    page = client.get("/")
    assert page.status_code == 200
    assert "KINTSUGI-Ω Arena" in page.text
    assert client.get("/board.js").status_code == 200
    assert client.get("/board.css").headers["content-type"].startswith("text/css")
    state = client.get("/api/state")
    assert state.status_code == 200
    assert state.headers["cache-control"] == "no-store"
    assert client.get("/api/does-not-exist").status_code == 404


# ---- the board's state ----------------------------------------------------------------------


def test_the_state_before_t0_reads_committed_facts_only(tmp_path: Path) -> None:
    state = svc.arena_state(_cfg(tmp_path, sabotage=False))
    assert state["window"] == "before T0"
    assert state["ledger"] == {"seq": 0, "h": None}
    assert state["incidents"] == [] and state["escalations"] == []  # noqa: PT018
    assert state["open"] is False
    fleet = {row["host"]: row for row in state["fleet"]}
    assert sorted(fleet) == ["ledger", "scout", "triage"]  # hosts/omega is not fleet traffic
    assert fleet["ledger"]["tasks"] == 360 and fleet["ledger"]["meets_slo"] is True  # noqa: PT018
    assert fleet["triage"]["external_tools"] == ["apply_label", "comment"]
    immunity = state["immunity"]
    assert immunity["rows"]["AB-1@1.1"]["crm-practice"] == "protected"
    assert immunity["rows"]["AB-1@1.1"]["scout"] == "excluded"  # the refinement's negative
    curve = state["learning_curve"]
    assert curve["status"] == "fitted"
    assert [p["n"] for p in curve["points"]] == [1, 2, 3, 4]
    assert curve["points"][1]["ref"] == "INC-R235459-0001"


@pytest.fixture
def w_repo(tmp_path: Path) -> tuple[Path, str]:
    """A scratch clone at T0: the window record and the ledger branch's genesis entry, written
    by the real ``kintsugi t0 genesis`` path (no monkeypatching)."""
    from datetime import UTC, datetime, timedelta

    from kintsugi.control.genesis import write_genesis
    from kintsugi.proof import attest
    from kintsugi.sandbox import proc
    from tests.helpers.checkpoints import git

    if proc.which("git") is None:
        pytest.skip("git is not installed")
    origin, path = tmp_path / "origin.git", tmp_path / "omega"
    proc.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], timeout_s=30, check=True)
    proc.run(["git", "init", "-q", "-b", "main", str(path)], timeout_s=30, check=True)
    git(path, "config", "user.name", "Surya Prasanth R")
    git(path, "config", "user.email", "suryaprasanthcse@gmail.com")
    (path / ".kintsugi").mkdir()
    (path / ".kintsugi" / "holdout.sha256").write_text("# manifest\n", encoding="utf-8")
    signer = attest.Ed25519Signer.generate()
    (path / ".kintsugi" / "attest.pub").write_text(signer.public_key_openssh() + "\n",
                                                   encoding="utf-8")  # fmt: skip
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    git(path, "remote", "add", "origin", str(origin))
    git(path, "push", "-q", "origin", "main")
    t0 = (datetime.now(UTC) - timedelta(minutes=1)).replace(microsecond=0)
    made = write_genesis(path, github_repo="o/r", rulesets=[7], t0=t0.isoformat())
    return path, made.genesis


def test_the_state_inside_w_reads_the_real_ledger_branch(
    tmp_path: Path, w_repo: tuple[Path, str]
) -> None:
    from kintsugi.proof.ledger_branch import BranchLedger

    repo, genesis = w_repo
    ledger = BranchLedger(repo, genesis)
    ledger.append({"type": "incident", "incident": "INC-0001", "host": "ledger"})
    ledger.append({"type": "transition", "incident": "INC-0001",
                   "transition": {"source": "OPEN", "target": "TRIAGED"}})  # fmt: skip
    ledger.append({"type": "escalate", "incident": "INC-0001", "reason": "no repair budget (L5)"})
    ledger.append({"type": "note", "text": "not an incident entry"})
    cfg = svc.ArenaConfig(repo=REPO, ledger_repo=repo, state=tmp_path / "arena")
    loaded = svc.load_ledger(cfg)
    assert isinstance(loaded, BranchLedger)
    assert [e["type"] for e in loaded.entries()] == ["genesis", "incident", "transition",
                                                     "escalate", "note"]  # fmt: skip
    state = svc.arena_state(cfg)
    assert state["window"] == "open"
    assert state["ledger"] == {"seq": 5, "h": loaded.entries()[-1]["hash"]}
    assert [i["type"] for i in state["incidents"]] == ["escalate", "transition", "incident"]
    assert [i["seq"] for i in state["incidents"]] == [4, 3, 2]  # the ledger's own numbering
    assert state["incidents"][1]["summary"] == "OPEN → TRIAGED"
    assert state["escalations"][0]["reason"] == "no repair budget (L5)"


def test_without_a_window_record_the_ledger_is_empty(tmp_path: Path) -> None:
    cfg = svc.ArenaConfig(repo=REPO, ledger_repo=tmp_path, state=tmp_path / "arena")
    assert svc.load_ledger(cfg).entries() == ()


def test_the_incident_stream_is_bounded_and_newest_first() -> None:
    entries = tuple({"type": "park", "incident": f"INC-{i:04d}", "reason": "r"}
                    for i in range(80))  # fmt: skip
    stream = svc.incident_stream(entries)
    assert len(stream) == svc.STREAM
    assert stream[0]["incident"] == "INC-0079" and stream[0]["seq"] == 80  # noqa: PT018


def test_the_immunity_matrix_follows_each_hosts_latest_event(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(REPO / "antibodies", tmp_path / "antibodies")
    lineage = tmp_path / "antibodies" / "AB-1" / "lineage.json"
    data = json.loads(lineage.read_text(encoding="utf-8"))
    data["events"].append({"event": "state", "host": "triage", "to": "QUARANTINED"})
    lineage.write_text(json.dumps(data), encoding="utf-8")
    matrix = svc.immunity_matrix(AntibodyLibrary(tmp_path / "antibodies"))
    assert matrix["rows"]["AB-1@1.1"]["triage"] == "quarantined"
    assert "triage" in matrix["hosts"]


# ---- the real factory: Saboteur.seal + the campaign signal ----------------------------------


def test_a_closed_arena_answers_503_without_spending_a_limit(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, sabotage=False)
    client = TestClient(svc.build_app(cfg))
    closed = client.post("/api/sabotage", json=GOOD)
    assert closed.status_code == 503
    assert "closed" in closed.json()["error"]
    assert client.post("/api/sabotage", json={**GOOD, "note": "x"}).status_code == 400
    arena = client.get("/api/state").json()["arena"]
    assert arena["accepted_today"] == 0
    assert set(arena["classes"]) == set(PARAMS)
    assert arena["classes"]["env.rate_limit"]["properties"]["rate"]["maximum"] == 0.5


def test_an_open_arena_seals_then_signals_the_campaign(tmp_path: Path) -> None:
    queued: list[tuple[str, float]] = []
    cfg = _cfg(tmp_path, sabotage=True)
    client = TestClient(svc.build_app(cfg, queue=lambda sid, d: queued.append((sid, d))))
    created = client.post("/api/sabotage", json=GOOD)
    assert created.status_code == 201, created.text
    answer = created.json()
    assert answer["id"] == "S-A0001" and answer["c"].startswith("sha256:")  # noqa: PT018
    assert queued == [("S-A0001", NOT_BEFORE_S)]
    public = json.loads((tmp_path / "store" / "S-A0001.json").read_text(encoding="utf-8"))
    assert public["c"] == answer["c"]
    assert (tmp_path / "store" / "S-A0001.age").is_file()  # the spec is sealed, not in clear
    assert client.post("/api/sabotage", json=GOOD).status_code == 429  # one per 10 minutes
    state = client.get("/api/state").json()
    assert state["open"] is True and state["arena"]["accepted_today"] == 1  # noqa: PT018


def test_a_sealed_request_the_campaign_did_not_take_answers_503_and_spends_its_id(
    tmp_path: Path,
) -> None:
    def down(spec_id: str, delay: float) -> None:
        raise ConnectionError("temporal is down")

    service = svc.build_service(_cfg(tmp_path, sabotage=True), queue=down)
    with pytest.raises(svc.Unavailable) as error:
        service.submit(GOOD, "203.0.113.7")
    assert error.value.answer["id"] == "S-A0001"
    assert error.value.answer["c"].startswith("sha256:")
    with pytest.raises(svc.Unavailable) as again:  # another client: a fresh id, never reused
        service.submit(GOOD, "198.51.100.1")
    assert again.value.answer["id"] == "S-A0002"
    assert again.value.answer["c"] != error.value.answer["c"]


def test_the_temporal_queue_signals_the_running_campaign() -> None:
    from kintsugi.control.activities import CREW
    from kintsugi.control.workflows import Campaign, SabotageCampaign
    from tests.helpers.control import FAST, Scenario, crew_for, temporal

    async def scenario() -> dict[str, str]:
        async with temporal(crew_for(Scenario())) as (_env, client):
            handle = await client.start_workflow(
                SabotageCampaign.run, args=[Campaign(specs=[("S-17", 3600.0)]), FAST],
                id=svc.CAMPAIGN_WORKFLOW_ID, task_queue=CREW)  # fmt: skip
            target = client.service_client.config.target_host
            queue = svc.TemporalQueue(target, svc.CAMPAIGN_WORKFLOW_ID)
            try:
                await asyncio.to_thread(queue, "S-A0001", 60.0)  # from another thread and loop
            finally:
                queue.close()
            await handle.signal(SabotageCampaign.close)
            await handle.result()
            return await handle.query(SabotageCampaign.commitments)

    commitments = asyncio.run(scenario())
    assert set(commitments) == {"S-17", "S-A0001"}


def test_the_config_comes_from_the_w_runtime(tmp_path: Path) -> None:
    sab = {"store": "s", "identity": "i", "recipient": "age1x"}
    data = {"repo": str(tmp_path), "t0_key": "k", "state": str(tmp_path / "state"), "sabotage": sab}
    runtime = RuntimeConfig.from_json(data)
    cfg = svc.ArenaConfig.from_runtime(runtime, temporal="wsl:7233")
    assert cfg.open
    assert cfg.state_dir == tmp_path / "state" / "arena"
    assert cfg.temporal == "wsl:7233" and cfg.campaign_id == "sabotage-campaign"  # noqa: PT018
    closed = svc.ArenaConfig.from_runtime(RuntimeConfig.from_json({"repo": str(tmp_path),
                                                                   "t0_key": "k"}))  # fmt: skip
    assert not closed.open


# ---- kintsugi arena serve -------------------------------------------------------------------


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import uvicorn

    seen: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        seen.update(kwargs, app=app)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.delenv("KINTSUGI_RUNTIME", raising=False)
    return seen


def test_arena_serve_without_a_runtime_serves_a_closed_board(
    served: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.chdir(REPO)
    monkeypatch.setattr(svc, "STATE_DIR", tmp_path / "arena-state")  # absolute: outside REPO
    assert main(["arena", "serve", "--port", "8899"]) == 0
    assert (served["host"], served["port"]) == ("127.0.0.1", 8899)
    assert "closed (board only)" in capsys.readouterr().out
    client = TestClient(served["app"])
    assert client.get("/").status_code == 200
    assert client.post("/api/sabotage", json=GOOD).status_code == 503


def test_arena_serve_with_a_runtime_opens_the_arena(
    served: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    _secret, recipient = new_identity()
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "repo": str(REPO), "t0_key": str(tmp_path / "t0"), "state": str(tmp_path / "state"),
        "sabotage": {"store": str(tmp_path / "store"), "identity": str(tmp_path / "id"),
                     "recipient": recipient}}), encoding="utf-8")  # fmt: skip
    assert main(["arena", "serve", "--runtime", str(runtime), "--temporal", "127.0.0.1:1",
                 "--campaign", "camp-x", "--daily-cap", "2"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "open" in out and "camp-x on 127.0.0.1:1" in out  # noqa: PT018
    assert (tmp_path / "state" / "arena" / "arena.sqlite").is_file()
    state = TestClient(served["app"]).get("/api/state").json()
    assert state["open"] is True and state["arena"]["daily_cap"] == 2  # noqa: PT018
