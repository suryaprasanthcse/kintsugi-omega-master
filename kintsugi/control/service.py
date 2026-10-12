"""Wiring for ``kintsugi control``: the supervisor, the queue forwarder, the Watchdog, T8 export.

State lives under ``.state/control/`` and ``.state/watchdog/`` (gitignored): the step ledger, the
attempt log, the chaos token (256-bit, never printed), the heartbeat and the pages.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.control import watchdog as w
from kintsugi.control.activities import CREW, SENSES, SURGEON

CONTROL_DIR = Path(".state") / "control"
WATCHDOG_DIR = Path(".state") / "watchdog"


async def export_history(
    target: str, workflow_id: str, out: Path, run_id: str | None = None
) -> int:
    """T8: the server's own event history of a workflow, as JSON (``temporal workflow show``)."""
    from temporalio.client import Client

    client = await Client.connect(target)
    history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(history.to_json(), encoding="utf-8", newline="\n")
    return len(history.events)


async def start_supervisor(target: str, config: c.ControlConfig) -> str:
    from temporalio.client import Client
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from kintsugi.control.workflows import FleetSupervisor

    client = await Client.connect(target)
    try:
        handle = await client.start_workflow(FleetSupervisor.run, args=[config, None],
                                             id=config.supervisor_id, task_queue=CREW)  # fmt: skip
        return f"started {handle.id} run {handle.result_run_id}"
    except WorkflowAlreadyStartedError:
        return f"{config.supervisor_id} is already running"


async def forward(target: str, store_path: Path, supervisor_id: str, *, once: bool) -> int:
    from temporalio.client import Client

    from kintsugi.control.hits import Forwarder, supervisor_deliver
    from kintsugi.senses.store import SenseStore

    client = await Client.connect(target)
    forwarder = Forwarder(SenseStore(store_path), supervisor_deliver(client, supervisor_id))
    if once:
        return await forwarder.drain()
    stop = threading.Event()
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        await forwarder.run(stop)
    return forwarder.delivered


def chaos_token(repo: Path) -> str:
    path = repo / WATCHDOG_DIR / "chaos.token"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return token


# What the worker pools need beyond the gateway's allowlist: the W runtime file (Stage 13a) and
# the neatlogs Public API credentials the crew reads failing traces with. Passed explicitly.
POOL_ENV = ("KINTSUGI_RUNTIME", "NEATLOGS_PROJECT_ID", "NEATLOGS_SERVICE_TOKEN",
            "NEATLOGS_OAUTH_TOKEN")  # fmt: skip


def pool_env(source: Mapping[str, str]) -> dict[str, str]:
    return {key: source[key] for key in POOL_ENV if source.get(key)}


def build_watchdog(repo: Path, target: str, crew: str, *, ingress_admin: str | None,
                   admin_token: str | None, env: Mapping[str, str] | None = None,
                   ) -> w.Watchdog:  # fmt: skip
    """The standard pools: crew + senses in one process, the Surgeons in their own (§6.6)."""
    from kintsugi.senses.service import STORE_PATH
    from kintsugi.senses.store import SenseStore

    pager = w.Pager(repo / WATCHDOG_DIR / "pages.jsonl")
    supervisor = w.Supervisor(pager)
    base = [sys.executable, "-m", "kintsugi.control.worker", "--target", target, "--crew", crew,
            "--ledger", str(repo / CONTROL_DIR / "steps.sqlite")]  # fmt: skip
    extra = pool_env(os.environ if env is None else env)
    supervisor.add(w.Pool("crew", [*base, "--pool", "crew", "--pool", "senses"], extra_env=extra,
                          log=repo / WATCHDOG_DIR / "crew.log"))  # fmt: skip
    supervisor.add(w.Pool("surgeon", [*base, "--pool", "surgeon"], extra_env=extra,
                          log=repo / WATCHDOG_DIR / "surgeon.log"))  # fmt: skip
    checks = {
        "temporal-pollers": w.temporal_pollers_check(target, [CREW, SURGEON, SENSES]),
        "senses-queue": w.queue_check(SenseStore(repo / STORE_PATH)),
        "entire-graph": w.command_check(["entire", "graph", "doctor", "--json"]),
        "neatlogs-sdk": w.command_check(
            [sys.executable, "-m", "neatlogs", "doctor", "--local", "--json"]
        ),
    }
    toggle = w.http_toggle(ingress_admin, admin_token) if ingress_admin and admin_token else None
    return w.Watchdog(
        supervisor,
        pager,
        checks,
        heartbeat_path=repo / WATCHDOG_DIR / "heartbeat",
        ingress_toggle=toggle,
        chaos_log=repo / WATCHDOG_DIR / "chaos.jsonl",
    )


def run_watchdog(dog: w.Watchdog, token: str, *, port: int) -> None:
    import uvicorn

    stop = threading.Event()
    threading.Thread(target=dog.loop, args=(stop,), daemon=True).start()
    try:
        uvicorn.run(w.chaos_app(dog, token), host="127.0.0.1", port=port, log_level="warning")
    finally:
        stop.set()
        dog.supervisor.stop()


def summary(dog: w.Watchdog) -> dict[str, Any]:
    return {name: {"pid": dog.supervisor.pid(name), "starts": pool.starts, "paged": pool.paged}
            for name, pool in dog.supervisor.pools.items()}  # fmt: skip
