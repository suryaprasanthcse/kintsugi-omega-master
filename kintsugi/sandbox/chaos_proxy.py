"""The seeded chaos proxy in front of every host's "external" services (spec §5.10, App. B).

Every tool call a host makes to an external service (the FX API, the document store, the mock
GitHub) goes through ``/{service}/{path}`` on this proxy. Faults are drawn from a profile:

- ``status``: answer with an HTTP error (e.g. 429) **without** calling the upstream, so a faulted
  call to a side-effecting service has no side effect;
- ``latency``: add a uniform delay;
- ``schema_drift``: rename fields in a JSON response (or, with ``change``, rename, drop or
  retype every object's first field);
- ``partial``: truncate a JSON list (or a text body) to a fraction;
- ``pagination``: return only the first ``page_size`` items of a JSON list, with a
  ``next_cursor`` in ``cursor_format`` the host has never seen.

Environment sabotage (``env.*``) arrives at run time as overlay rules (``chaos_overlay``),
drawn under the sabotage's own key so the background profile's draws never move. A ``status``
rule with ``burst`` > 1 fails a run of calls once it fires; a ``latency`` rule with ``p50_ms``
and ``p95_ms`` draws a log-normal delay through those percentiles.

Determinism: whether rule *r* fires on a call is ``u < rate`` with

    u = HMAC-SHA256(seed, r ‖ service ‖ task ‖ fingerprint(request) ‖ occurrence) / 2^64

so the same seed produces the same faults at the same positions on every run, independent of
thread scheduling: concurrent tasks never share an occurrence counter (calls carry their task
id in ``x-kintsugi-task``). The background profile (`profiles/background.yaml`) is disclosed;
sabotage profiles are sealed by the Saboteur (Stage 8).
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import math
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Literal

import httpx
import yaml

from kintsugi.sandbox.cassette import (
    Player,
    Recorder,
    fingerprint,
    request_record,
    response_record,
    to_response,
)
from kintsugi.sandbox.httpserve import Handler, HttpRequest, HttpResponse, forwardable

if TYPE_CHECKING:
    from kintsugi.sandbox.chaos_overlay import ChaosOverlay

PROFILES = Path(__file__).with_name("profiles")
CHAOS_HEADER = "x-kintsugi-chaos"
FaultKind = Literal["status", "latency", "schema_drift", "partial", "pagination"]
FAULTS = {"status", "latency", "schema_drift", "partial", "pagination"}


@dataclass(frozen=True)
class Rule:
    service: str  # glob over service names
    fault: FaultKind
    rate: float
    path: str = "*"  # glob over the path inside the service
    params: Mapping[str, Any] = field(default_factory=dict)

    def applies(self, service: str, path: str) -> bool:
        return fnmatch.fnmatchcase(service, self.service) and fnmatch.fnmatchcase(path, self.path)


@dataclass(frozen=True)
class Profile:
    name: str
    seed: int
    rules: tuple[Rule, ...]
    disclosed: bool = True

    def with_seed(self, seed: int) -> Profile:
        return Profile(self.name, seed, self.rules, self.disclosed)


def load_profile(path: Path) -> Profile:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = []
    for raw in data.get("rules") or []:
        raw = dict(raw)
        service, fault, rate = raw.pop("service"), raw.pop("fault"), float(raw.pop("rate"))
        if fault not in FAULTS:
            raise ValueError(f"unknown fault kind {fault!r}")
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"rate must be within [0, 1], got {rate}")
        rules.append(Rule(service, fault, rate, str(raw.pop("path", "*")), raw))
    return Profile(str(data["name"]), int(data.get("seed", 0)), tuple(rules),
                   bool(data.get("disclosed", True)))  # fmt: skip


def background_profile() -> Profile:
    return load_profile(PROFILES / "background.yaml")


def unit(seed: int, *parts: object) -> float:
    """A deterministic uniform draw in [0, 1) from the seed and the call's coordinates."""
    message = "\x1f".join(str(p) for p in parts).encode("utf-8")
    mac = hmac.new(str(seed).encode("ascii"), message, hashlib.sha256).digest()
    return int.from_bytes(mac[:8], "big") / 2**64


@dataclass(frozen=True)
class Decision:
    rule: int | str  # the profile's rule index, or an overlay rule's sabotage key
    fault: FaultKind
    params: Mapping[str, Any]
    draw: float  # the uniform draw that fired (or set the latency)


def _fires(seed: int, draw_key: int | str, rule: Rule, service: str, task: str, key: str,
           occurrence: int) -> bool:  # fmt: skip
    burst = int(rule.params.get("burst", 1)) if rule.fault == "status" else 1
    for back in range(max(burst, 1)):  # a firing call starts a run of `burst` failures
        if occurrence - back < 0:
            break
        if unit(seed, draw_key, service, task, key, occurrence - back) < rule.rate:
            return True
    return False


def decide(
    profile: Profile,
    service: str,
    path: str,
    task: str,
    key: str,
    occurrence: int,
    overlay: Sequence[tuple[str, Rule]] = (),
) -> list[Decision]:
    fired: list[Decision] = []
    keyed: list[tuple[int | str, Rule]] = [*enumerate(profile.rules), *overlay]
    for draw_key, rule in keyed:
        if not rule.applies(service, path):
            continue
        if _fires(profile.seed, draw_key, rule, service, task, key, occurrence):
            jitter = unit(profile.seed, "jitter", draw_key, service, task, key, occurrence)
            fired.append(Decision(draw_key, rule.fault, rule.params, jitter))
    return fired


def latency_s(decision: Decision) -> float:
    """A latency fault's delay: uniform over ``latency_ms``, or log-normal through p50/p95."""
    params = decision.params
    if "p50_ms" in params:
        p50, p95 = float(params["p50_ms"]), float(params["p95_ms"])
        sigma = 0.0 if p95 <= p50 else (math.log(p95 / p50) / NormalDist().inv_cdf(0.95))
        z = NormalDist().inv_cdf(min(max(decision.draw, 1e-9), 1 - 1e-9))
        return p50 * math.exp(sigma * z) / 1000.0
    low, high = (params.get("latency_ms") or [0, 0])[:2]
    return (low + (high - low) * decision.draw) / 1000.0


def _error_body(status: int) -> dict[str, Any]:
    kinds = {429: "rate_limit_error", 500: "api_error", 502: "bad_gateway", 503: "overloaded"}
    return {"type": "error", "error": {"type": kinds.get(status, "upstream_error"),
                                       "message": f"chaos: injected HTTP {status}"}}  # fmt: skip


def _rename(value: Any, renames: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {renames.get(k, k): _rename(v, renames) for k, v in value.items()}
    if isinstance(value, list):
        return [_rename(v, renames) for v in value]
    return value


def _truncate(value: Any, field_name: str | None, keep: float) -> Any:
    if field_name is None:
        if isinstance(value, list):
            return value[: int(len(value) * keep)]
        if isinstance(value, str):
            return value[: int(len(value) * keep)]
        return value
    if isinstance(value, dict) and isinstance(value.get(field_name), list | str):
        return {**value, field_name: _truncate(value[field_name], None, keep)}
    return value


def _first_key(obj: dict[str, Any]) -> str | None:
    return sorted(obj)[0] if obj else None


def _drift(value: Any, change: str) -> Any:
    """``env.schema_drift``: every object's first field (sorted) is renamed, dropped or retyped."""
    if isinstance(value, list):
        return [_drift(v, change) for v in value]
    if not isinstance(value, dict):
        return value
    name = _first_key(value)
    out = {k: _drift(v, change) for k, v in value.items()}
    if name is None:
        return out
    if change == "rename_field":
        out[f"{name}_v2"] = out.pop(name)
    elif change == "drop_field":
        out.pop(name)
    elif change == "type_change":
        current = out[name]
        out[name] = str(current) if not isinstance(current, str) else [current]
    return out


def _paginate(value: Any, page_size: int, cursor_format: str) -> Any:
    """``env.pagination_change``: the first page of the response's list and a new cursor."""
    cursors = {"opaque": "b3BhcXVlOnBhZ2UtMg", "offset": page_size,
               "timestamp": "2026-10-12T00:00:00Z"}  # fmt: skip
    cursor = cursors.get(cursor_format, cursors["opaque"])
    if isinstance(value, list):
        return {"items": value[:page_size], "next_cursor": cursor}
    if isinstance(value, dict):
        for name in sorted(value):
            if isinstance(value[name], list):
                return {**value, name: value[name][:page_size], "next_cursor": cursor}
    return value


def apply_body_faults(response: HttpResponse, decisions: list[Decision]) -> HttpResponse:
    """Apply schema-drift, partial-response and pagination faults to a JSON (or text) body."""
    shaping = [d for d in decisions if d.fault in {"schema_drift", "partial", "pagination"}]
    if not shaping or not response.body:
        return response
    try:
        body: Any = json.loads(response.body.decode("utf-8"))
        is_json = True
    except ValueError:
        body, is_json = response.body.decode("utf-8", errors="replace"), False
    for d in shaping:
        if d.fault == "schema_drift" and "change" in d.params:
            body = _drift(body, str(d.params["change"])) if is_json else body
        elif d.fault == "schema_drift":
            body = _rename(body, dict(d.params.get("rename") or {}))
        elif d.fault == "pagination":
            body = (_paginate(body, int(d.params["page_size"]), str(d.params["cursor_format"]))
                    if is_json else body)  # fmt: skip
        else:
            body = _truncate(body, d.params.get("field"), float(d.params.get("keep", 0.5)))
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if is_json else str(body).encode()
    return HttpResponse(response.status, dict(response.headers), raw)


Upstream = str | Handler  # a base URL, or an in-process handler (mock services, tests)


class ChaosProxy:
    """``/{service}/{path}`` → the service's upstream, with seeded faults; record or replay."""

    def __init__(
        self,
        upstreams: Mapping[str, Upstream],
        profile: Profile,
        *,
        recorder: Recorder | None = None,
        player: Player | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 30.0,
        live: frozenset[str] = frozenset(),
        overlay: ChaosOverlay | None = None,
    ) -> None:
        if recorder is not None and player is not None:
            raise ValueError("record or replay, not both")
        self.upstreams = dict(upstreams)
        self.profile = profile
        self.overlay = overlay  # environment sabotage delivered at run time (§7.2)
        self.recorder = recorder
        self.player = player
        # Services that hold the host's own state (a books API is the agent's database, not the
        # world): never recorded, never replayed, so a replay recomputes their state against the
        # code under test instead of serving the state the failing code left behind (A-50).
        self.live = live
        self._sleep = sleep
        self._seen: Counter[tuple[str, str, str]] = Counter()
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=timeout_s)
        self.log: list[dict[str, Any]] = []  # one row per call: what fired where

    def __call__(self, request: HttpRequest) -> HttpResponse:
        service, _, rest = request.path.lstrip("/").partition("/")
        path = "/" + rest
        if service not in self.upstreams:
            return HttpResponse.of_json(404, _error_body(404) | {"service": service})
        record = request_record(request, path=path)
        if self.player is not None:  # replay: the recorded answer, faults included
            if service in self.live:
                return self._forward(service, path, request)
            return to_response(self.player.play(service, request.task, record).response)

        key = fingerprint(record)
        with self._lock:
            occurrence = self._seen[(service, request.task, key)]
            self._seen[(service, request.task, key)] += 1
        extra = self.overlay.rules() if self.overlay is not None else ()
        decisions = decide(self.profile, service, path, request.task, key, occurrence, extra)
        started = time.monotonic()
        for d in decisions:
            if d.fault == "latency":
                self._sleep(latency_s(d))
        status_fault = next((d for d in decisions if d.fault == "status"), None)
        if status_fault is not None:
            status = int(status_fault.params.get("status", 429))
            headers = {"retry-after": str(status_fault.params.get("retry_after_s", 0))}
            response = HttpResponse.of_json(status, _error_body(status), headers)
        else:
            response = apply_body_faults(self._forward(service, path, request), decisions)
        fired = ",".join(f"{d.fault}#{d.rule}" for d in decisions)
        if fired:
            response.headers[CHAOS_HEADER] = fired
        latency_ms = int((time.monotonic() - started) * 1000)
        row = {"service": service, "path": path, "task": request.task, "occurrence": occurrence,
               "status": response.status, "fired": fired}  # fmt: skip
        sabotage = sorted({str(d.params["sabotage"]) for d in decisions if "sabotage" in d.params})
        if sabotage:
            row["sabotage"] = ",".join(sabotage)
        self.log.append(row)
        if self.recorder is not None and service not in self.live:
            self.recorder.record(service, request.task, record, response_record(response),
                                 latency_ms)  # fmt: skip
        return response

    def _forward(self, service: str, path: str, request: HttpRequest) -> HttpResponse:
        upstream = self.upstreams[service]
        inner = HttpRequest(request.method, path, request.query, request.headers, request.body)
        if callable(upstream):
            return upstream(inner)
        reply = self._client.request(
            request.method, upstream.rstrip("/") + path, params=dict(request.query),
            content=request.body, headers=forwardable(request.headers),
        )  # fmt: skip
        headers = {k: v for k, v in reply.headers.items() if k.lower() != "content-encoding"}
        return HttpResponse(reply.status_code, headers, reply.content)  # content is decoded

    def close(self) -> None:
        self._client.close()
