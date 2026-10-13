"""The Arena service (Stage 11): the real ``ArenaService`` behind ``kintsugi arena serve``.

``build_service`` binds the Arena API's ports to the runtime:

- **seal**: ``Saboteur.seal`` over the W runtime's sabotage store (outside every repository) for
  the T0-held age recipient (``RuntimeConfig.sabotage``). Without a sabotage store the Arena is
  **closed**: the board renders, and a valid request answers 503 without spending a limit.
- **queue**: the ``SabotageCampaign.request`` signal to the running campaign workflow
  (``sabotage-campaign`` by default) on the Temporal server, sent from a dedicated event loop so
  the HTTP handler never blocks on Temporal's loop.
- **state**: ``arena_state``, the board's one input, composed from committed and runtime facts
  only: the fleet (``hosts/*/host.yaml`` and ``evidence/baselines``), the incident stream and
  the head of the ledger (the W ledger branch once the window record exists, nothing before),
  the immunity matrix (each antibody's lineage), the learning curve (the Wright refit in
  ``business/cfo_export/wright.json``) and the escalations.

The board itself is ``arena/`` (static HTML, CSS and JavaScript; no build step, no CDN): the
app serves it at ``/`` beside ``GET /api/state`` and ``POST /api/sabotage``.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from kintsugi.arena_api import (
    DAILY_CAP,
    ArenaService,
    Limiter,
    Unavailable,
    board_state,
    create_app,
    fleet_tools,
)
from kintsugi.crew.runtime import RuntimeConfig, SabotageConfig
from kintsugi.memory.library import AntibodyLibrary

CAMPAIGN_WORKFLOW_ID = "sabotage-campaign"
BOARD_DIR = Path("arena")
STATE_DIR = Path(".state") / "arena"
INCIDENT_TYPES = ("incident", "transition", "escalate", "park", "rewind", "revert", "treatment",
                  "economics", "vaccination", "antibody", "sabotage", "eval-augment")  # fmt: skip
STREAM = 50  # newest ledger entries in the incident stream


@dataclass(frozen=True)
class ArenaConfig:
    repo: Path
    sabotage: SabotageConfig | None = None  # None: the Arena is closed (board only)
    temporal: str = "localhost:7233"
    campaign_id: str = CAMPAIGN_WORKFLOW_ID
    state: Path | None = None  # default: <repo>/.state/arena
    daily_cap: int = DAILY_CAP
    window: Path | None = None  # default: <ledger_repo>/.kintsugi/window.json
    ledger_repo: Path | None = None  # the clone holding refs/heads/ledger (default: repo)

    @classmethod
    def from_runtime(cls, runtime: RuntimeConfig, **overrides: Any) -> ArenaConfig:
        return cls(repo=runtime.repo, sabotage=runtime.sabotage,
                   state=runtime.state_dir / "arena", window=runtime.window_path,
                   **overrides)  # fmt: skip

    @property
    def state_dir(self) -> Path:
        return self.state or Path(self.repo) / STATE_DIR

    @property
    def open(self) -> bool:
        return self.sabotage is not None


class TemporalQueue:
    """The ``queue`` port: ``SabotageCampaign.request(spec_id, delay_s)`` from any thread."""

    def __init__(
        self,
        target: str,
        workflow_id: str = CAMPAIGN_WORKFLOW_ID,
        *,
        timeout_s: float = 15.0,
        connect: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.target = target
        self.workflow_id = workflow_id
        self.timeout_s = timeout_s
        self._connect = connect
        self._client: Any = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="arena-temporal",
                                        daemon=True)  # fmt: skip
        self._thread.start()

    async def _signal(self, spec_id: str, delay_s: float) -> None:
        from kintsugi.control.workflows import SabotageCampaign

        if self._client is None:
            if self._connect is not None:
                self._client = await self._connect(self.target)
            else:
                from temporalio.client import Client

                self._client = await Client.connect(self.target)
        handle = self._client.get_workflow_handle(self.workflow_id)
        await handle.signal(SabotageCampaign.request, args=[spec_id, float(delay_s)])

    def __call__(self, spec_id: str, delay_s: float) -> None:
        future = asyncio.run_coroutine_threadsafe(self._signal(spec_id, delay_s), self._loop)
        future.result(timeout=self.timeout_s)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def closed_seal(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    raise Unavailable("the Arena is closed: requests open during the autonomy window W")


def closed_queue(spec_id: str, delay_s: float) -> None:
    raise Unavailable("the Arena is closed: no sabotage campaign is running")


class _NoLedger:
    """Before T0 there is no ledger branch: the board shows an empty head."""

    def entries(self) -> tuple[dict[str, Any], ...]:
        return ()


def load_ledger(cfg: ArenaConfig) -> Any:
    """The W ledger branch once the window record exists, read-only; otherwise an empty one."""
    from kintsugi.proof.audit import WINDOW_FILE, Window
    from kintsugi.proof.ledger_branch import BranchLedger

    repo = Path(cfg.ledger_repo or cfg.repo)
    path = cfg.window or repo / WINDOW_FILE
    if not path.is_file():
        return _NoLedger()
    window = Window.from_json(json.loads(path.read_text(encoding="utf-8")))
    return BranchLedger(repo, window.genesis(), branch=window.ledger_ref)


def _json(path: Path) -> dict[str, Any] | None:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


TERMINAL = ("RESOLVED", "ESCALATED", "PARKED")
CANARY_OUTCOMES = {"RESOLVED": "accepted", "REPAIRING": "rejected (re-entered repair)",
                   "ESCALATED": "rejected (escalated)"}  # fmt: skip


def host_activity(entries: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    """Per host, from the ledger: its open incidents and its last canary outcome (§7.7)."""
    host_of: dict[str, str] = {}
    state: dict[str, str] = {}
    closed: set[str] = set()
    canary: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(entries, 1):
        incident = entry.get("incident")
        if not isinstance(incident, str):
            continue
        if entry.get("host"):
            host_of.setdefault(incident, str(entry["host"]))
        kind = entry.get("type")
        if kind in ("escalate", "park"):
            closed.add(incident)
        transition = entry.get("transition")
        if kind == "transition" and isinstance(transition, Mapping):
            target = str(transition.get("target"))
            state[incident] = target
            if transition.get("source") == "CANARY" and incident in host_of:
                canary[host_of[incident]] = {
                    "incident": incident, "at": entry.get("at"), "seq": entry.get("seq", position),
                    "outcome": CANARY_OUTCOMES.get(target, target.lower()),
                }  # fmt: skip
        if kind == "revert" and incident in host_of:
            canary[host_of[incident]] = {"incident": incident, "at": entry.get("at"),
                                         "seq": entry.get("seq", position),
                                         "outcome": "rejected (reverted)"}  # fmt: skip
    activity: dict[str, dict[str, Any]] = {}
    for incident, host in host_of.items():
        row = activity.setdefault(host, {"open_incidents": [], "last_canary": None})
        if incident not in closed and state.get(incident) not in TERMINAL:
            row["open_incidents"].append(incident)
    for host, last in canary.items():
        activity.setdefault(host, {"open_incidents": [], "last_canary": None})["last_canary"] = last
    return activity


def fleet(repo: Path, entries: tuple[dict[str, Any], ...] = ()) -> list[dict[str, Any]]:
    """The fleet board: each host's tier, SLO, recorded baseline (health card), open incidents
    and last canary outcome."""
    from kintsugi.fleet.hosts import load_hosts

    activity = host_activity(entries)
    rows = []
    for host, spec in sorted(load_hosts(repo).items()):
        card = _json(Path(repo) / "evidence" / "baselines" / f"{host}.json") or {}
        pooled = card.get("pooled") or {}
        live = activity.get(host) or {"open_incidents": [], "last_canary": None}
        rows.append({
            "host": host, "description": spec.description, "tier": spec.economics.tier,
            "slo_pass_rate": spec.slo.pass_rate, "pass_rate": pooled.get("pass_rate"),
            "wilson95": pooled.get("wilson95"), "tasks": pooled.get("n"),
            "meets_slo": card.get("meets_slo"), "p95_latency_s": card.get("p95_latency_s"),
            "model_mode": card.get("model_mode"), "recorded_at": card.get("recorded_at"),
            "external_tools": sorted(n for n, t in spec.tools.items() if t.external),
            "open_incidents": len(live["open_incidents"]),
            "open_incident_ids": sorted(live["open_incidents"]),
            "last_canary": live["last_canary"],
        })  # fmt: skip
    return rows


def _summary(entry: Mapping[str, Any]) -> str:
    transition = entry.get("transition")
    if isinstance(transition, Mapping):
        return f"{transition.get('source')} → {transition.get('target')}"
    for key in ("reason", "event", "outcome", "state"):
        if entry.get(key):
            return str(entry[key])[:200]
    return str(entry.get("type", ""))


def incident_stream(entries: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """The newest incident-shaped ledger entries, newest first."""
    stream = []
    for position, entry in enumerate(entries, 1):
        if entry.get("type") not in INCIDENT_TYPES:
            continue
        seq = entry.get("seq", position)  # the ledger numbers its own entries
        stream.append({"seq": seq, "at": entry.get("at"), "type": entry.get("type"),
                       "incident": entry.get("incident") or entry.get("sabotage"),
                       "host": entry.get("host"), "summary": _summary(entry)})  # fmt: skip
    return stream[-STREAM:][::-1]


def immunity_matrix(library: AntibodyLibrary, fleet_hosts: Iterable[str] = ()) -> dict[str, Any]:
    """Antibody × host (§7.7's heatmap): each host's latest status in the antibody's lineage.
    A host the latest motif scan matched without protection yet is ``vulnerable``; a fleet
    host the antibody never touched is ``n/a``."""
    rows: dict[str, dict[str, str]] = {}
    hosts: set[str] = set(fleet_hosts)
    for ab_id in library.ids():
        lineage = _json(library.root / ab_id / "lineage.json") or {}
        ab = library.load(ab_id)
        cells: dict[str, str] = {}
        for event in lineage.get("events") or []:
            host, kind = event.get("host"), event.get("event")
            if not host:
                continue
            if kind == "state" and event.get("to"):
                cells[host] = str(event["to"]).lower()
            elif kind in ("protected", "unprotected"):
                cells[host] = str(kind)
            elif kind == "induced-trigger" and host not in cells:
                cells[host] = "triggered"
        for refinement in lineage.get("refinements") or []:
            for host in refinement.get("negatives") or []:
                cells[host] = "excluded"
        for host in lineage.get("hosts_protected") or ab.lineage.hosts_protected:
            cells[host] = "protected"
        scans = [e for e in lineage.get("events") or [] if e.get("event") == "scan"]
        for host in (scans[-1].get("matched") or []) if scans else []:
            if cells.get(host) in (None, "triggered", "unprotected"):
                cells[host] = "vulnerable"  # matched by the latest scan, not protected yet
        rows[ab.ref] = cells
        hosts.update(cells)
    for cells in rows.values():
        for host in hosts:
            cells.setdefault(host, "n/a")
    return {"hosts": sorted(hosts), "rows": rows}


def learning_curve(repo: Path) -> dict[str, Any]:
    """Response cost against response number, with the Wright refit (Stage 9)."""
    fit = _json(Path(repo) / "business" / "cfo_export" / "wright.json") or {}
    points = [
        {"n": p.get("n"), "cost_usd": p.get("cost_usd"), "ref": ref}
        for p, ref in zip(fit.get("points") or [], fit.get("refs") or [], strict=False)
    ]
    usable = [p for p in points if isinstance(p["n"], int | float) and p["n"] > 0
              and isinstance(p["cost_usd"], int | float) and p["cost_usd"] > 0]  # fmt: skip
    log_points = [{"ln_n": round(math.log(p["n"]), 6), "ln_cost": round(math.log(p["cost_usd"]), 6)}
                  for p in usable]  # fmt: skip
    return {"points": points, "log_points": log_points, "empirical_slope": loglog_slope(usable),
            "b": fit.get("b"), "c1": fit.get("c1"), "r2": fit.get("r2"),
            "progress_ratio": fit.get("progress_ratio"), "status": fit.get("status", "missing"),
            "phases": fit.get("phases") or []}  # fmt: skip


def loglog_slope(points: Iterable[Mapping[str, Any]]) -> float | None:
    """The least-squares slope of ln(cost) on ln(n): Wright's law makes it −b."""
    xy = [(math.log(float(p["n"])), math.log(float(p["cost_usd"]))) for p in points]
    if len({x for x, _ in xy}) < 2:
        return None
    mx = sum(x for x, _ in xy) / len(xy)
    my = sum(y for _, y in xy) / len(xy)
    num = sum((x - mx) * (y - my) for x, y in xy)
    den = sum((x - mx) ** 2 for x, _ in xy)
    return round(num / den, 6)


def last_anchor(ledger: Any) -> dict[str, Any] | None:
    anchors = ledger.anchors() if hasattr(ledger, "anchors") else []
    if not anchors:
        return None
    last = anchors[-1]
    return {"tag": last.tag, "seq": last.seq, "h": last.h}


def arena_state(cfg: ArenaConfig, library: AntibodyLibrary | None = None) -> dict[str, Any]:
    library = library or AntibodyLibrary(Path(cfg.repo) / "antibodies")
    ledger = load_ledger(cfg)
    entries = tuple(ledger.entries())
    state = board_state(ledger, library)
    state["ledger"]["anchor"] = last_anchor(ledger)
    board = fleet(Path(cfg.repo), entries)
    state.update({
        "open": cfg.open,
        "window": "open" if not isinstance(ledger, _NoLedger) else "before T0",
        "fleet": board,
        "incidents": incident_stream(entries),
        "immunity": immunity_matrix(library, [row["host"] for row in board]),
        "learning_curve": learning_curve(Path(cfg.repo)),
    })  # fmt: skip
    return state


def build_service(cfg: ArenaConfig, *, queue: Callable[[str, float], None] | None = None,
                  clock: Callable[[], float] | None = None) -> ArenaService:  # fmt: skip
    """The real Arena: Saboteur.seal + the SabotageCampaign.request signal (or closed)."""
    from kintsugi.crew.saboteur import Saboteur

    tools = fleet_tools(Path(cfg.repo))
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    limiter = Limiter(cfg.state_dir / "arena.sqlite", daily_cap=cfg.daily_cap)
    saboteur = (Saboteur(cfg.sabotage.store, (cfg.sabotage.recipient,), tuple(tools))
                if cfg.sabotage is not None else None)  # fmt: skip

    def taken(spec_id: str) -> bool:  # a spec id already sealed in the store is never reused
        return saboteur is not None and saboteur.record(spec_id) is not None

    if saboteur is not None:
        seal: Callable[[Mapping[str, Any]], Mapping[str, Any]] = saboteur.seal
        port = queue or TemporalQueue(cfg.temporal, cfg.campaign_id)
    else:
        seal, port = closed_seal, queue or closed_queue
    extra = {"clock": clock} if clock is not None else {}
    return ArenaService(tools, limiter, seal, port, state=lambda: arena_state(cfg), taken=taken,
                        **extra)  # fmt: skip


def build_app(cfg: ArenaConfig, *, trust_forwarded: bool = False,
              queue: Callable[[str, float], None] | None = None) -> FastAPI:  # fmt: skip
    board = Path(cfg.repo) / BOARD_DIR
    return create_app(build_service(cfg, queue=queue), trust_forwarded=trust_forwarded,
                      board=board if board.is_dir() else None)  # fmt: skip
