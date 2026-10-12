"""``kintsugi control`` wiring: T8 history export, the supervisor start, the chaos token."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kintsugi.control import contracts as c
from kintsugi.control import service as svc
from kintsugi.control import watchdog as w
from kintsugi.control.activities import CREW
from kintsugi.control.workflows import Incident
from tests.helpers.control import FAST, Scenario, crew_for, temporal


def test_history_export_and_an_idempotent_supervisor_start(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, list[str]]:
        crew = crew_for(Scenario())
        async with temporal(crew) as (env, client):
            alert = c.Alert("INC-T8", "ledger", ())
            await client.execute_workflow(Incident.run, args=[alert, FAST], id="INC-T8",
                                          task_queue=CREW)  # fmt: skip
            target = env.client.service_client.config.target_host
            events = await svc.export_history(target, "INC-T8", tmp_path / "INC-T8.json")
            starts = [await svc.start_supervisor(target, c.ControlConfig()) for _ in range(2)]
            await client.get_workflow_handle("fleet-supervisor").terminate("test over")
        return events, starts

    events, starts = asyncio.run(scenario())
    exported = json.loads((tmp_path / "INC-T8.json").read_text(encoding="utf-8"))
    assert len(exported["events"]) == events > 10
    assert exported["events"][0]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"
    assert starts[0].startswith("started fleet-supervisor")
    assert starts[1] == "fleet-supervisor is already running"


def test_worker_pools_receive_the_runtime_file_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    added: list[w.Pool] = []
    monkeypatch.setattr(w.Supervisor, "add", lambda self, pool: added.append(pool))  # no start
    env = {"KINTSUGI_RUNTIME": "C:/runtime.json", "NEATLOGS_PROJECT_ID": "p",
           "ANTHROPIC_API_KEY": "never-forwarded", "NEATLOGS_OAUTH_TOKEN": ""}  # fmt: skip
    svc.build_watchdog(tmp_path, "localhost:7233", "kintsugi.crew.runtime:crew",
                       ingress_admin=None, admin_token=None, env=env)  # fmt: skip
    assert [pool.name for pool in added] == ["crew", "surgeon"]
    for pool in added:
        assert dict(pool.extra_env) == {"KINTSUGI_RUNTIME": "C:/runtime.json",
                                        "NEATLOGS_PROJECT_ID": "p"}  # fmt: skip
        assert "kintsugi.crew.runtime:crew" in pool.argv


def test_the_chaos_token_is_generated_once_and_kept(tmp_path: Path) -> None:
    token = svc.chaos_token(tmp_path)
    assert len(token) >= 43
    assert svc.chaos_token(tmp_path) == token
