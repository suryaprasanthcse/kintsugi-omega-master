"""§6.6 / Appendix B: the Saboteur delivers ``healer.*`` faults through the Watchdog's chaos API.

S-18 (``healer.kill_worker`` on the Surgeon pool together with ``healer.webhook_outage``) is
sealed as one spec, opened by the delivery lane and sent to the real chaos app: the Surgeon pool
is killed and restarted by the supervisor, the ingress goes dark for the outage and comes back,
and the ledger records one delivery. A fault with no chaos endpoint is refused, never faked.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kintsugi.control import watchdog as w
from kintsugi.control.activities import DeliveryRefused
from kintsugi.crew.factory import FileLedger, OmegaCrew, SabotageLane
from kintsugi.crew.healer import HealerPort, pool_name
from kintsugi.crew.saboteur import Saboteur, compose
from kintsugi.proof import sealed
from kintsugi.proof.ledger import genesis_hash

TOKEN = "t" * 43
SLEEP = [sys.executable, "-c", "import time; time.sleep(120)"]
NOW = datetime.now(UTC).replace(microsecond=0)


def _s18(**target: Any) -> dict[str, Any]:
    params = {
        "pool": "surgeon-workers",
        "together": [{"class": "healer.webhook_outage", "duration_s": 0.4}],
        **target,
    }
    return compose("S-98", "healer.kill_worker", host="practice",
                   framing="Worker maintenance window",
                   ground_truth={"files": [], "mechanism": "kill and outage"},
                   not_before=(NOW - timedelta(minutes=2)).isoformat(), seed=18,
                   params=params)  # fmt: skip


@pytest.fixture
def chaos(tmp_path: Path) -> Any:
    """The real chaos app over a supervisor with a crew and a Surgeon pool."""
    switches: list[bool] = []
    pager = w.Pager(tmp_path / "pages.jsonl")
    sup = w.Supervisor(pager)
    sup.add(w.Pool("crew", SLEEP))
    sup.add(w.Pool("surgeon", SLEEP))
    dog = w.Watchdog(sup, pager, ingress_toggle=switches.append,
                     chaos_log=tmp_path / "chaos.jsonl")  # fmt: skip
    api = TestClient(w.chaos_app(dog, TOKEN))
    token = tmp_path / "repo" / ".state" / "watchdog" / "chaos.token"
    token.parent.mkdir(parents=True)
    token.write_text(TOKEN, encoding="utf-8")

    def post(url: str, body: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[int, Any]:
        response = api.post(url.removeprefix("http://127.0.0.1:8790"), json=dict(body),
                            headers=dict(headers))  # fmt: skip
        return response.status_code, response.json()

    port = HealerPort.for_repo(tmp_path / "repo")
    port.post = post
    yield SimpleNamespace(port=port, sup=sup, switches=switches, log=tmp_path / "chaos.jsonl")
    sup.stop()


def test_compose_routes_healer_classes_to_the_chaos_api() -> None:
    assert _s18()["delivery"]["mode"] == "chaos_api"
    assert compose("S-97", "env.latency", host="practice", framing="f",
                   ground_truth={}, not_before=NOW.isoformat(), seed=1,
                   params={"tool": "x", "p50_ms": 1, "p95_ms": 2})["delivery"]["mode"] \
        == "chaos_proxy"  # fmt: skip
    assert pool_name("surgeon-workers") == "surgeon"


def test_s18_is_delivered_through_the_sealed_lane_to_the_chaos_api(
    chaos: Any, tmp_path: Path
) -> None:
    secret, recipient = sealed.new_identity()
    saboteur = Saboteur(tmp_path / "store", (recipient,), ("practice",))
    spec = _s18()
    saboteur.seal(spec)
    lane = SabotageLane(saboteur, secret, deliver=lambda req, host: "never",
                        healer=chaos.port.deliver)  # fmt: skip
    ledger = FileLedger(tmp_path / "ledger.jsonl", genesis_hash("t0", b"\0" * 32))
    crew = SimpleNamespace(ledger=ledger, sabotage=lane, _lane=lambda: lane)
    surgeon, crew_pool = chaos.sup.pid("surgeon"), chaos.sup.pid("crew")
    OmegaCrew.do_deliver_sabotage(crew, None, "S-98")  # type: ignore[arg-type]
    OmegaCrew.do_deliver_sabotage(crew, None, "S-98")  # type: ignore[arg-type]  (a retry)
    assert chaos.sup.tick() == ["surgeon"]  # killed once, restarted by the Watchdog (§6.6)
    assert chaos.sup.pid("surgeon") != surgeon
    assert chaos.sup.pid("crew") == crew_pool  # the delivering pool is never the target
    assert chaos.switches == [False]
    time.sleep(1.0)
    assert chaos.switches == [False, True]  # the ingress comes back after the outage
    [entry] = list(ledger.entries())
    assert (entry["event"], entry["mode"]) == ("delivered", "chaos_api")
    assert entry["ref"] == f"kill:surgeon:pid={surgeon};ingress-outage:0.4s"
    ops = [line for line in chaos.log.read_text().splitlines() if line.strip()]
    assert len(ops) == 2
    assert all('"sabotage_id": "S-98"' in op for op in ops)


def test_what_the_chaos_api_cannot_do_is_refused_before_any_call(chaos: Any) -> None:
    pids = chaos.sup.pid("surgeon")
    with pytest.raises(DeliveryRefused, match="no chaos endpoint"):
        chaos.port.deliver({**_s18(), "class": "healer.entire_hook_failure"})
    with pytest.raises(DeliveryRefused, match="no chaos endpoint"):
        chaos.port.deliver(_s18(together=[{"class": "healer.neatlogs_ingest_delay",
                                           "delay_s": 60}]))  # fmt: skip
    with pytest.raises(DeliveryRefused, match="runs the delivery"):
        chaos.port.deliver(_s18(pool="crew", together=[]))
    with pytest.raises(DeliveryRefused, match="not a healer class"):
        chaos.port.deliver({**_s18(), "class": "env.latency"})
    with pytest.raises(DeliveryRefused, match=r"refused \(404\)"):
        chaos.port.deliver(_s18(pool="nope", together=[]))
    assert chaos.sup.pid("surgeon") == pids  # nothing was killed
    assert chaos.switches == []


def test_a_missing_token_is_retried_not_refused(tmp_path: Path) -> None:
    port = HealerPort.for_repo(tmp_path)  # the Watchdog has not written its token yet
    with pytest.raises(RuntimeError, match="no chaos token"):
        port.deliver(_s18(together=[]))
