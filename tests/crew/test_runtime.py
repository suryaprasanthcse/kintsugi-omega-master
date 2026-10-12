"""The W crew factory (Stage 13a step 1): the branch ledger, the T0 signer, anchors, the lane."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kintsugi.control.genesis import write_genesis
from kintsugi.control.ship import LocalShipper
from kintsugi.crew import runtime
from kintsugi.crew.notary import Notary
from kintsugi.crew.runtime import RuntimeConfig, RuntimeConfigError, SabotageConfig
from kintsugi.crew.upstream import UpstreamLane
from kintsugi.proof import attest
from kintsugi.proof.audit import WINDOW_FILE
from kintsugi.proof.ledger_branch import BranchLedger
from kintsugi.proof.sealed import new_identity
from kintsugi.sandbox import proc
from tests.helpers.checkpoints import git


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    origin = tmp_path / "origin.git"
    proc.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], timeout_s=30, check=True)
    repo = tmp_path / "omega"
    proc.run(["git", "init", "-q", "-b", "main", str(repo)], timeout_s=30, check=True)
    git(repo, "config", "user.name", "Surya Prasanth R")
    git(repo, "config", "user.email", "suryaprasanthcse@gmail.com")
    (repo / ".kintsugi").mkdir()
    (repo / ".kintsugi" / "holdout.sha256").write_text("# manifest\n", encoding="utf-8")
    signer = attest.Ed25519Signer.generate()
    keys = tmp_path / "keys"
    keys.mkdir()
    signer.save(keys / "attest-t0", keys / "attest-t0.pub")
    (repo / ".kintsugi" / "attest.pub").write_text(signer.public_key_openssh() + "\n",
                                                   encoding="utf-8")  # fmt: skip
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    t0 = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0)
    write_genesis(repo, github_repo="o/r", rulesets=[7], t0=t0.isoformat())
    cfg = RuntimeConfig(repo=repo, t0_key=keys / "attest-t0", crew_model="offline",
                        state=tmp_path / "state")  # fmt: skip
    return {"repo": repo, "origin": origin, "signer": signer, "keys": keys, "cfg": cfg,
            "tmp": tmp_path}  # fmt: skip


def test_the_crew_appends_to_the_ledger_branch_and_pushes_signed_anchors(
    world: dict[str, Any],
) -> None:
    crew = runtime.build(world["cfg"])
    assert isinstance(crew.ledger, BranchLedger)
    assert isinstance(crew.shipper, LocalShipper)  # no GitHub: practice runs only
    assert crew.sabotage is None
    wired = runtime.describe(crew)
    assert wired["ledger"] == "refs/heads/ledger"
    assert wired["seq"] == 1
    assert wired["model"] == "offline"
    assert wired["signer"] == attest.fingerprint(
        attest.load_public_key(world["signer"].public_key_openssh())
    )
    step: Any = None
    crew.do_ledger_append(step, {"type": "transition", "incident": "INC-0001"})
    tag = crew.do_anchor_ledger(step, 0)
    assert tag.startswith("anchor-")
    post = (world["tmp"] / "state" / "anchors" / f"{tag}.txt").read_text(encoding="utf-8")
    assert post.startswith(f"KINTSUGI-Ω ledger anchor {tag}\nseq 2  h ")
    repo, origin = world["repo"], world["origin"]
    assert git(origin, "rev-parse", "refs/heads/ledger") == git(repo, "rev-parse", "ledger")
    assert git(origin, "rev-parse", f"refs/tags/{tag}^{{commit}}") == git(repo, "rev-parse",
                                                                          "ledger")  # fmt: skip
    entries = BranchLedger(repo, crew.ledger.genesis).entries()
    assert [e["type"] for e in entries] == ["genesis", "transition"]


def test_the_factory_refuses_what_would_break_a_proof(world: dict[str, Any]) -> None:
    cfg: RuntimeConfig = world["cfg"]
    other = attest.Ed25519Signer.generate()
    other.save(world["keys"] / "other", world["keys"] / "other.pub")
    with pytest.raises(RuntimeConfigError, match="attest_pubkey"):
        runtime.build(RuntimeConfig(repo=cfg.repo, t0_key=world["keys"] / "other",
                                    crew_model="offline"))  # fmt: skip
    inside = cfg.repo / "attest-t0"
    inside.write_bytes(cfg.t0_key.read_bytes())
    with pytest.raises(RuntimeConfigError, match="inside a git worktree"):
        runtime.build(RuntimeConfig(repo=cfg.repo, t0_key=inside, crew_model="offline"))
    inside.unlink()
    record = json.loads((cfg.repo / WINDOW_FILE).read_text(encoding="utf-8"))
    closed = world["tmp"] / "closed.json"
    closed.write_text(json.dumps({**record, "t1": record["t0"]}), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="closed"):
        runtime.build(RuntimeConfig(repo=cfg.repo, t0_key=cfg.t0_key, window=closed))
    with pytest.raises(RuntimeConfigError, match="is missing"):
        runtime.build(RuntimeConfig(repo=cfg.repo, t0_key=cfg.t0_key,
                                    window=world["tmp"] / "missing.json"))  # fmt: skip
    git(cfg.repo, "update-ref", "-d", "refs/heads/ledger")
    with pytest.raises(RuntimeConfigError, match="no genesis entry"):
        runtime.build(cfg)


def test_the_sabotage_lane_delivers_through_the_upstream_lane(world: dict[str, Any]) -> None:
    cfg: RuntimeConfig = world["cfg"]
    secret, recipient = new_identity()
    identity = world["keys"] / "t0.agekey"
    identity.write_text(secret + "\n", encoding="ascii")
    sab = SabotageConfig(world["tmp"] / "store", identity, recipient)
    with_lane = RuntimeConfig(repo=cfg.repo, t0_key=cfg.t0_key, sabotage=sab)
    notary = Notary(world["signer"])
    with pytest.raises(RuntimeConfigError, match="GitHub"):
        runtime.sabotage_lane(with_lane, notary, None, ("ledger",))
    lane = runtime.sabotage_lane(with_lane, notary, object(), ("ledger",))
    assert lane is not None
    assert lane.identity == secret
    assert lane.saboteur.recipients == (recipient,)
    assert isinstance(getattr(lane.deliver, "__self__", None), UpstreamLane)
    assert lane.publish is not None
    leaked = SabotageConfig(world["tmp"] / "store", cfg.repo / "t0.agekey", recipient)
    (cfg.repo / "t0.agekey").write_text(secret, encoding="ascii")
    with pytest.raises(RuntimeConfigError, match="inside a git worktree"):
        runtime.sabotage_lane(RuntimeConfig(repo=cfg.repo, t0_key=cfg.t0_key, sabotage=leaked),
                              notary, object(), ("ledger",))  # fmt: skip


def test_the_runtime_file_and_the_check_command(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg: RuntimeConfig = world["cfg"]
    monkeypatch.delenv(runtime.ENV, raising=False)
    with pytest.raises(RuntimeConfigError, match=runtime.ENV):
        runtime.crew()
    path = world["tmp"] / "runtime.json"
    path.write_text(json.dumps({"repo": str(cfg.repo), "t0_key": str(cfg.t0_key),
                                "crew_model": "offline", "state": str(cfg.state)}),
                    encoding="utf-8")  # fmt: skip
    assert runtime.main(["check", "--runtime", str(path)]) == 0
    wired = json.loads(capsys.readouterr().out)
    assert wired["seq"] == 1
    assert wired["anchors_pushed_to"] == "origin"
    with pytest.raises(RuntimeConfigError, match="crew_model"):
        RuntimeConfig.from_json({"repo": ".", "t0_key": "k", "crew_model": "gpt"})
    assert runtime.first_hour(datetime(2026, 10, 13, 2, 30, tzinfo=UTC)) == 497738
