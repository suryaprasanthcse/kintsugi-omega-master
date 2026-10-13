"""The Arena's public API (Stage 11, spec §7.7): "Sabotage it", strictly compiled and sealed.

Anyone may ask Omega's fleet to be attacked, but a request never becomes code, a prompt or free
text inside the system:

- **Strict schema.** A request is JSON with exactly ``host``, ``class`` and ``params``. Only the
  environment classes of the Appendix B catalog are open to the public (``env.*``, delivered by
  the chaos proxy); the patch, prompt, model, data and healer classes are refused. Every
  parameter is an enum or a bounded number (``tool`` must be one of the host's external tools).
  Unknown fields, free text and out-of-range values are rejected; nothing is coerced.
- **Sealed on arrival.** A valid request is composed into a §7.2 spec (``saboteur.compose``,
  with a fresh honeytoken and a ``not_before`` in the future), sealed through the ``seal`` port
  (``Saboteur.seal``) and queued through the ``queue`` port (the ``SabotageCampaign.request``
  signal). The answer is the public commitment: id, ``c`` and ``not_before``, never the spec.
- **Rate limits.** One accepted request per client per 10 minutes and a daily cap across all
  clients, kept in SQLite. Clients are keyed by a salted SHA-256 of their address, never the
  address itself.
- **Read side.** ``GET /api/state`` is the Arena board's only input (``kintsugi.arena_service``
  composes it: fleet, incident stream, immunity matrix, learning curve, ledger head, escalations,
  antibodies) plus the "Sabotage it" form's limits.
- **Unavailable.** Before W the seal port is closed and a valid request answers 503 without
  spending a limit; a request sealed but not taken by the campaign answers 503 with its public
  commitment (the id is spent: sealing an id again returns the earlier commitment).
- **Board.** ``create_app(..., board=Path("arena"))`` serves the static board (``arena/``: plain
  HTML, CSS and JavaScript, no build step, no external requests) at ``/``.

The model-assisted compiler of the plan (natural language → this schema) is not wired in this
edition; the deterministic schema is the only way in. Chaos-proxy deliveries are wired at Stage
13 (MASTER_HANDOFF 13a step 5): until then an accepted request is sealed and queued, and its
delivery waits on that wiring.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kintsugi.crew.saboteur import CATALOG, compose

WINDOW_S = 600.0  # one request per client per 10 minutes
DAILY_CAP = 20
NOT_BEFORE_S = 300.0  # sealed now, delivered no sooner than five minutes later
FAULT_DURATION_S = 1800.0  # an Arena fault stays active for 30 minutes once delivered
FRAMING = "Arena request"

Rate = Annotated[float, Field(ge=0.01, le=0.5, strict=True)]
Millis = Annotated[int, Field(ge=10, le=30000, strict=True)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RateLimit(_Strict):
    tool: str
    rate: Rate
    burst: Annotated[int, Field(ge=1, le=10, strict=True)]


class Latency(_Strict):
    tool: str
    p50_ms: Millis
    p95_ms: Millis


class SchemaDrift(_Strict):
    tool: str
    change: Literal["rename_field", "drop_field", "type_change"]


class PartialResponse(_Strict):
    tool: str
    truncation_pct: Annotated[int, Field(ge=5, le=90, strict=True)]


class PaginationChange(_Strict):
    tool: str
    page_size: Annotated[int, Field(ge=1, le=100, strict=True)]
    cursor_format: Literal["opaque", "offset", "timestamp"]


PARAMS: dict[str, type[_Strict]] = {
    "env.rate_limit": RateLimit,
    "env.latency": Latency,
    "env.schema_drift": SchemaDrift,
    "env.partial_response": PartialResponse,
    "env.pagination_change": PaginationChange,
}


class ArenaRequest(_Strict):
    host: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9-]*$")
    cls: str = Field(alias="class", min_length=1, max_length=64)
    params: dict[str, Any]


class Refused(ValueError):
    """The request is not something the Arena may compile (400)."""


class Limited(RuntimeError):
    """A rate limit or the daily cap refused the request (429)."""


class Unavailable(RuntimeError):
    """The Arena cannot take requests now: closed before W, or the campaign is unreachable (503).
    ``answer`` carries the public commitment when the request was sealed but not queued."""

    def __init__(self, message: str, answer: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.answer = dict(answer or {})


@dataclass(frozen=True)
class Compiled:
    host: str
    cls: str
    params: dict[str, Any]


def compile_request(payload: Any, tools: Mapping[str, Sequence[str]]) -> Compiled:
    """``tools``: each fleet host → its external tools. Anything else is refused."""
    if not isinstance(payload, dict):
        raise Refused("the request must be a JSON object")
    try:
        request = ArenaRequest.model_validate(payload)
    except ValidationError as error:
        raise Refused(f"not the Arena schema: {error.errors()[0]['msg']}") from error
    if request.host not in tools:
        raise Refused(f"{request.host!r} is not a fleet host")
    if request.cls not in CATALOG:
        raise Refused(f"{request.cls!r} is not in the fault catalog")
    model = PARAMS.get(request.cls)
    if model is None:
        raise Refused(f"{request.cls} is not open to the public (environment classes only)")
    try:
        params = model.model_validate(request.params).model_dump()
    except ValidationError as error:
        raise Refused(f"bad parameters: {error.errors()[0]['msg']}") from error
    if params["tool"] not in tools[request.host]:
        raise Refused(f"{params['tool']!r} is not an external tool of {request.host}")
    if set(params) != set(CATALOG[request.cls]):
        raise Refused("the parameters do not match the catalog entry")  # defence in depth
    if request.cls == "env.latency" and params["p95_ms"] < params["p50_ms"]:
        raise Refused("p95 must not be below p50")
    return Compiled(request.host, request.cls, params)


def fleet_tools(repo: Path) -> dict[str, list[str]]:
    """Each fleet host's external tools, from its host.yaml."""
    from kintsugi.fleet.hosts import load_hosts

    return {host: sorted(name for name, tool in spec.tools.items() if tool.external)
            for host, spec in load_hosts(repo).items()}  # fmt: skip


class Limiter:
    """One accepted request per client per window, and a daily cap, in SQLite."""

    def __init__(self, path: Path | str, *, window_s: float = WINDOW_S, daily_cap: int = DAILY_CAP,
                 salt: str | None = None) -> None:  # fmt: skip
        self.path = str(path)
        self.window_s = window_s
        self.daily_cap = daily_cap
        self.salt = salt or secrets.token_hex(16)
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS accepted (client TEXT NOT NULL, at REAL NOT "
                       "NULL, day TEXT NOT NULL, spec TEXT NOT NULL)")  # fmt: skip

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10.0)

    def client(self, address: str) -> str:
        return hashlib.sha256(f"{self.salt}:{address}".encode()).hexdigest()

    def check(self, address: str, now: float) -> None:
        day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        with self._lock, self._db() as db:
            last = db.execute("SELECT MAX(at) FROM accepted WHERE client = ?",
                              (self.client(address),)).fetchone()[0]  # fmt: skip
            if last is not None and now - float(last) < self.window_s:
                wait = int(self.window_s - (now - float(last)))
                raise Limited(
                    f"one request per {int(self.window_s // 60)} minutes: retry in {wait}s"
                )
            today = db.execute("SELECT COUNT(*) FROM accepted WHERE day = ?", (day,)).fetchone()[0]
            if int(today) >= self.daily_cap:
                raise Limited(f"today's cap of {self.daily_cap} requests is reached")

    def record(self, address: str, now: float, spec_id: str) -> None:
        day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        with self._lock, self._db() as db:
            db.execute("INSERT INTO accepted VALUES (?, ?, ?, ?)",
                       (self.client(address), now, day, spec_id))  # fmt: skip

    def count(self) -> int:
        with self._db() as db:
            return int(db.execute("SELECT COUNT(*) FROM accepted").fetchone()[0])

    def today(self, now: float) -> int:
        day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        with self._db() as db:
            return int(db.execute("SELECT COUNT(*) FROM accepted WHERE day = ?",
                                  (day,)).fetchone()[0])  # fmt: skip


class ArenaService:
    def __init__(
        self,
        tools: Mapping[str, Sequence[str]],
        limiter: Limiter,
        seal: Callable[[Mapping[str, Any]], Mapping[str, Any]],  # Saboteur.seal
        queue: Callable[[str, float], None],  # SabotageCampaign.request (spec id, delay)
        *,
        state: Callable[[], Mapping[str, Any]] = dict,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.tools = tools
        self.limiter = limiter
        self.seal = seal
        self.queue = queue
        self.state = state
        self.clock = clock

    def submit(self, payload: Any, address: str) -> dict[str, Any]:
        now = self.clock()
        compiled = compile_request(payload, self.tools)  # refuse before touching any limit
        self.limiter.check(address, now)
        spec_id = f"S-A{self.limiter.count() + 1:04d}"
        not_before = datetime.fromtimestamp(now, UTC) + timedelta(seconds=NOT_BEFORE_S)
        spec = compose(spec_id, compiled.cls, host=compiled.host, framing=FRAMING,
                       ground_truth={"files": [], "mechanism": compiled.cls},
                       not_before=not_before.isoformat(), seed=int(now) % 1_000_000,
                       params=compiled.params, duration_s=FAULT_DURATION_S)  # fmt: skip
        record = self.seal(spec)
        # The id is spent once sealed: a later request must never reuse it, because sealing an
        # existing id returns the earlier commitment (Saboteur.seal is idempotent per id).
        self.limiter.record(address, now, spec_id)
        answer = {"id": spec_id, "c": record["c"], "not_before": record["not_before"],
                  "class": compiled.cls, "host": compiled.host}  # fmt: skip
        try:
            self.queue(spec_id, NOT_BEFORE_S)
        except Exception as error:
            raise Unavailable(f"{spec_id} is sealed but the campaign did not take it: "
                              f"{type(error).__name__}", answer) from error  # fmt: skip
        return answer

    def arena(self) -> dict[str, Any]:
        """The "Sabotage it" form's limits, for the board."""
        now = self.clock()
        return {"daily_cap": self.limiter.daily_cap, "accepted_today": self.limiter.today(now),
                "window_minutes": int(self.limiter.window_s // 60),
                "classes": {cls: model.model_json_schema() for cls, model in PARAMS.items()},
                "tools": {host: list(tools) for host, tools in self.tools.items()}}  # fmt: skip


def create_app(service: ArenaService, *, trust_forwarded: bool = False,
               board: Path | None = None) -> FastAPI:  # fmt: skip
    """The API, plus the static Arena board (``arena/``) at ``/`` when ``board`` is given."""
    from starlette.concurrency import run_in_threadpool

    app = FastAPI(title="kintsugi-arena", docs_url=None, redoc_url=None, openapi_url=None)

    def address(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "")
        if trust_forwarded and forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    @app.post("/api/sabotage")
    async def sabotage(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"error": "the body must be JSON"}, status_code=400)
        try:  # sealing and the campaign signal block: keep them off the event loop
            answer = await run_in_threadpool(service.submit, payload, address(request))
            return JSONResponse(answer, status_code=201)
        except Refused as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except Limited as error:
            return JSONResponse({"error": str(error)}, status_code=429)
        except Unavailable as error:
            return JSONResponse({"error": str(error), **error.answer}, status_code=503)

    @app.get("/api/state")
    async def state() -> JSONResponse:
        found = dict(await run_in_threadpool(service.state))
        found.setdefault("arena", service.arena())
        return JSONResponse(found, headers={"Cache-Control": "no-store"})

    if board is not None:
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=board, html=True), name="board")
    return app


def board_state(ledger: Any, library: Any) -> dict[str, Any]:
    """The Arena board's live inputs: ledger head, escalations, antibody states."""
    entries = ledger.entries()
    head = {"seq": len(entries), "h": entries[-1]["hash"] if entries else None}
    escalations = [{"incident": e.get("incident"), "reason": e.get("reason"), "at": e.get("at")}
                   for e in entries if e.get("type") == "escalate"][-20:]  # fmt: skip
    antibodies = {}
    for ab_id in library.ids():
        ab = library.load(ab_id)
        antibodies[ab.ref] = {"state": str(ab.state),
                              "hosts_protected": list(ab.lineage.hosts_protected)}  # fmt: skip
    return {"ledger": head, "escalations": escalations, "antibodies": antibodies}
