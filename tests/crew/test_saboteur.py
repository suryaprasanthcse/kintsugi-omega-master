"""The Saboteur on a disclosed practice spec: realism rules, the deterministic rubric, sealing
into an age store outside the repository, leak-free delivery, and audit C6 over the result.
No model is called: the rubric is the deterministic fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kintsugi.crew.factory import FileLedger, OmegaCrew, SabotageLane
from kintsugi.crew.notary import NotaryRequest
from kintsugi.crew.saboteur import (
    CATALOG,
    SabotageRefused,
    Saboteur,
    compose,
    deterministic_rubric,
    patch_sha256,
    realism_problems,
)
from kintsugi.proof import audit, sealed
from kintsugi.proof.audit import Status
from kintsugi.proof.ledger import genesis_hash

REPO = Path(__file__).resolve().parents[2]
FLEET = ("practice",)
PATCH = """--- a/hosts/practice/practice/fetch.py
+++ b/hosts/practice/practice/fetch.py
@@ -3,4 +3,4 @@ def fetch_all(client):
     try:
         return client.fetch()
-    except TimeoutError:
-        raise
+    except Exception:
+        return []
"""
NOW = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)  # before any real run of this suite


def _spec(patch: str = PATCH, **overrides: Any) -> dict[str, Any]:
    spec = compose(
        "S-90", "code.regression.exception_swallow", host="practice",
        framing="Simplify error handling in fetch_all", patch=patch,
        ground_truth={"files": ["hosts/practice/practice/fetch.py"], "symbols": ["fetch_all"],
                      "mechanism": "a broad except returns an empty list on any failure"},
        not_before=(NOW + timedelta(minutes=1)).isoformat(), seed=4242,
    )  # fmt: skip
    spec.update(overrides)
    return spec


@pytest.fixture
def identity() -> tuple[str, str]:
    return sealed.new_identity()


@pytest.fixture
def saboteur(tmp_path_factory: pytest.TempPathFactory, identity: tuple[str, str]) -> Saboteur:
    store = tmp_path_factory.mktemp("sabotage-store")
    return Saboteur(store, (identity[1],), FLEET, clock=lambda: NOW)


def test_the_catalog_is_appendix_b() -> None:
    assert len(CATALOG) == 17
    assert "code.regression.state_reset_on_retry" in CATALOG


def test_a_practice_spec_passes_the_rules_and_the_rubric() -> None:
    spec = _spec()
    assert realism_problems(spec, PATCH, fleet=FLEET) == []
    assert deterministic_rubric(spec, PATCH) == 1.0


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        (lambda s, p: ({**s, "class": "code.regression.rm_everything"}, p), "fault catalog"),
        (lambda s, p: ({**s, "target": {"host": "neighbour"}}, p), "blast radius"),
        (
            lambda s, p: (
                s,
                p.replace("hosts/practice/practice/fetch.py", "hosts/practice/tests/test_fetch.py"),
            ),
            "patch_sha256",
        ),
        (lambda s, p: (s, None), "there is none"),
        (
            lambda s, p: (
                {
                    **s,
                    "delivery": {**s["delivery"], "framing": f"Refactor, keep {s['honeytoken']}"},
                },
                p,
            ),
            "honeytoken",
        ),
        (
            lambda s, p: (
                {**s, "delivery": {**s["delivery"], "framing": "Inject the S-90 sabotage"}},
                p,
            ),
            "sabotage id",
        ),
    ],
    ids=["non-catalog", "outside-fleet", "patch-swapped", "no-patch", "leaky-framing", "named"],
)
def test_realism_rejects(change: Any, expect: str) -> None:
    spec, patch = change(_spec(), PATCH)
    problems = realism_problems(spec, patch, fleet=FLEET)
    assert any(expect in p for p in problems), problems


def test_protected_paths_and_destructive_operations_are_refused() -> None:
    to_tests = PATCH.replace("practice/practice/fetch.py", "practice/tests/test_fetch.py")
    problems = realism_problems(_spec(to_tests), to_tests, fleet=FLEET)
    assert any("protected paths" in p for p in problems)
    outside = PATCH.replace("hosts/practice/practice/fetch.py", "kintsugi/proof/audit.py")
    assert any("outside hosts/practice/" in p
               for p in realism_problems(_spec(outside), outside, fleet=FLEET))  # fmt: skip
    wiper = PATCH.replace("+        return []", "+        shutil.rmtree('/data')")
    assert any("destructive" in p for p in realism_problems(_spec(wiper), wiper, fleet=FLEET))


def test_seal_store_open_and_reveal(saboteur: Saboteur, identity: tuple[str, str]) -> None:
    spec = _spec()
    record = saboteur.seal(spec, PATCH)
    assert set(record) == {"id", "c", "sealed_at", "not_before", "class_family"}
    assert record["class_family"] == "code.regression"
    assert saboteur.seal(spec, PATCH) == record  # a retry never re-seals
    stored = b"".join(p.read_bytes() for p in saboteur.store.iterdir())
    assert spec["honeytoken"].encode() not in stored
    assert b"broad except" not in stored
    opened, seal, patch = saboteur.open("S-90", identity[0])
    assert opened == spec
    assert patch == PATCH
    assert sealed.verify_reveal(record["c"], opened, seal.salt)
    path, body = Saboteur.reveal(opened, seal)
    assert path == "sabotage-log/S-90.reveal.json"
    assert b'"salt"' in body


def test_sealing_refuses_a_low_rubric_and_a_store_inside_a_repo(
    saboteur: Saboteur, identity: tuple[str, str]
) -> None:
    elsewhere = compose("S-91", "code.regression.exception_swallow", host="practice",
                        framing="Tidy", patch=PATCH, ground_truth={"files": ["other.py"],
                        "mechanism": "m"}, not_before=NOW.isoformat(), seed=1)  # fmt: skip
    with pytest.raises(SabotageRefused, match="rubric"):
        saboteur.seal(elsewhere, PATCH)
    with pytest.raises(SabotageRefused, match="inside a git worktree"):
        Saboteur(REPO / "sabotage-store", (identity[1],), FLEET)


def test_delivery_carries_the_framing_and_the_patch_only(saboteur: Saboteur) -> None:
    spec = _spec()
    request = Saboteur.delivery_request(spec, PATCH)
    assert request.purpose == ("Simplify error handling in fetch_all. Apply this patch and run "
                               "the existing tests.")  # fmt: skip
    assert request.diff == PATCH
    assert "S-90" not in request.incident + request.subject + request.reference
    leaky = PATCH.replace("return []", f"return []  # {spec['honeytoken']}")
    with pytest.raises(SabotageRefused, match="honeytoken"):
        Saboteur.delivery_request({**spec, "delivery": {**spec["delivery"],
                                   "patch_sha256": patch_sha256(leaky)}}, leaky)  # fmt: skip


def test_the_commitment_request_publishes_only_the_record(saboteur: Saboteur) -> None:
    record = saboteur.seal(_spec(), PATCH)
    request = Saboteur.commitment_request(record)
    assert "+++ b/sabotage-log/S-90.json" in request.diff
    assert record["c"] in request.diff
    assert "honeytoken" not in request.diff


def test_crew_steps_seal_deliver_and_satisfy_c6(
    saboteur: Saboteur, identity: tuple[str, str], tmp_path: Path
) -> None:
    """The dry run: seal a practice spec, deliver it through a recorded port, reveal it, and
    let audit C6 check commitment, ordering and honeytoken."""
    spec = _spec()
    delivered: list[tuple[NotaryRequest, str]] = []
    published: list[NotaryRequest] = []
    lane = SabotageLane(saboteur, identity[0],
                        deliver=lambda req, host: delivered.append((req, host)) or "pr-1",
                        plans={"S-90": (spec, PATCH)},
                        publish=lambda req: published.append(req) or "sha")  # fmt: skip
    ledger = FileLedger(tmp_path / "ledger.jsonl", genesis_hash("t0", b"\0" * 32))
    crew = SimpleNamespace(ledger=ledger, sabotage=lane, _lane=lambda: lane)
    step: Any = None
    c = OmegaCrew.do_seal_sabotage(crew, step, "S-90")  # type: ignore[arg-type]
    assert OmegaCrew.do_seal_sabotage(crew, step, "S-90") == c  # type: ignore[arg-type]
    assert len(published) == 1
    OmegaCrew.do_deliver_sabotage(crew, step, "S-90")  # type: ignore[arg-type]
    assert delivered[0][1] == "practice"
    entries = list(ledger.entries())
    assert [e["event"] for e in entries] == ["sealed", "delivered"]
    _spec_back, seal, _patch = saboteur.open("S-90", identity[0])
    reveal = sealed.reveal_record(spec, seal)
    result = audit.c6_sabotage([saboteur.record("S-90") or {}], {"S-90": reveal}, entries,
                               lambda token: [], window_closed=True)  # fmt: skip
    assert result.status is Status.PASS, result.problems


def _env_spec() -> dict[str, Any]:
    return compose("S-A0001", "env.rate_limit", host="practice", framing="Arena request",
                   ground_truth={"files": [], "mechanism": "env.rate_limit"},
                   not_before=(NOW - timedelta(minutes=1)).isoformat(), seed=7,
                   params={"tool": "fetch", "rate": 0.2, "burst": 2},
                   duration_s=900.0)  # fmt: skip


def test_an_environment_spec_is_delivered_through_the_chaos_port(
    saboteur: Saboteur, identity: tuple[str, str], tmp_path: Path
) -> None:
    """An Arena request (env.*) has no patch: it goes to the chaos overlay, once."""
    spec = _env_spec()
    saboteur.seal(spec)
    injected: list[dict[str, Any]] = []
    lane = SabotageLane(
        saboteur,
        identity[0],
        deliver=lambda req, host: "never",
        chaos=lambda s: injected.append(dict(s)) or "overlay/S-A0001.json",
    )
    ledger = FileLedger(tmp_path / "ledger.jsonl", genesis_hash("t0", b"\0" * 32))
    crew = SimpleNamespace(ledger=ledger, sabotage=lane, _lane=lambda: lane)
    OmegaCrew.do_deliver_sabotage(crew, None, "S-A0001")  # type: ignore[arg-type]
    OmegaCrew.do_deliver_sabotage(crew, None, "S-A0001")  # type: ignore[arg-type]  (a retry)
    assert [s["target"]["tool"] for s in injected] == ["fetch"]  # delivered exactly once
    [entry] = list(ledger.entries())
    assert (entry["event"], entry["mode"], entry["ref"]) == ("delivered", "chaos_proxy",
                                                             "overlay/S-A0001.json")  # fmt: skip


def test_a_spec_without_a_delivery_port_is_refused_without_retries(
    saboteur: Saboteur, identity: tuple[str, str], tmp_path: Path
) -> None:
    from kintsugi.control.activities import NON_RETRYABLE, SPECS, DeliveryRefused

    saboteur.seal(_env_spec())
    lane = SabotageLane(saboteur, identity[0], deliver=lambda req, host: "never")  # no chaos
    ledger = FileLedger(tmp_path / "ledger.jsonl", genesis_hash("t0", b"\0" * 32))
    crew = SimpleNamespace(ledger=ledger, sabotage=lane, _lane=lambda: lane)
    with pytest.raises(DeliveryRefused, match="no delivery port"):
        OmegaCrew.do_deliver_sabotage(crew, None, "S-A0001")  # type: ignore[arg-type]
    assert DeliveryRefused in NON_RETRYABLE
    assert "DeliveryRefused" in SPECS["deliver_sabotage"].non_retryable
    assert list(ledger.entries()) == []
