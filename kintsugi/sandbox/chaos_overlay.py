"""Chaos-proxy delivery of environment sabotage (Appendix B ``env.*``, §7.2, §7.7).

Patch classes are delivered as an upstream change through a notary session; the five
environment classes are delivered here instead, as **overlay rules** for the chaos proxy that
already sits in front of every host's external services:

- ``ChaosOverlay.deliver(spec)`` compiles an opened spec into chaos rules on the target tool's
  external service and writes ``<folder>/<spec id>.json`` atomically, active from now until
  ``schedule.duration_s`` later. The folder lives under the runtime's state directory, outside
  the repository; the fleet's chaos proxy reads it on every call (``ChaosProxy(overlay=…)``).
- Overlay draws are keyed by the sabotage id, never by a rule index, so adding or expiring an
  overlay never shifts the background profile's seeded faults (A-24).

The mapping from catalog parameters to faults:

- ``env.rate_limit`` → ``status`` 429 at ``rate``; a firing call starts a run of ``burst``
  failing calls;
- ``env.latency`` → ``latency``, a log-normal delay through ``p50_ms`` and ``p95_ms``;
- ``env.schema_drift`` → ``schema_drift`` with ``change`` (rename_field, drop_field,
  type_change);
- ``env.partial_response`` → ``partial``, keeping ``100 − truncation_pct`` percent of a list
  or text;
- ``env.pagination_change`` → ``pagination``, pages of ``page_size`` with a
  ``cursor_format`` cursor.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kintsugi.sandbox.chaos_proxy import Rule

DEFAULT_DURATION_S = 1800.0  # an environment fault lasts 30 minutes unless the spec says so
OVERLAY_DIR = Path("chaos") / "active"  # under the runtime's state directory


class OverlayError(ValueError):
    pass


def compile_env(spec: Mapping[str, Any], external: Mapping[str, Mapping[str, str]]
                ) -> list[Rule]:  # fmt: skip
    """``external``: host → tool → the external service its calls go through."""
    cls = str(spec.get("class", ""))
    target = dict(spec.get("target") or {})
    host, tool = str(target.get("host", "")), str(target.get("tool", ""))
    service = (external.get(host) or {}).get(tool)
    if service is None:
        raise OverlayError(f"{tool!r} is not an external tool of {host!r}")
    sid = str(spec.get("id"))
    tag = {"sabotage": sid}
    if cls == "env.rate_limit":
        return [Rule(service, "status", float(target["rate"]), "*",
                     {**tag, "status": 429, "burst": int(target["burst"])})]  # fmt: skip
    if cls == "env.latency":
        return [Rule(service, "latency", 1.0, "*",
                     {**tag, "p50_ms": int(target["p50_ms"]),
                      "p95_ms": int(target["p95_ms"])})]  # fmt: skip
    if cls == "env.schema_drift":
        return [Rule(service, "schema_drift", 1.0, "*", {**tag, "change": str(target["change"])})]
    if cls == "env.partial_response":
        keep = 1.0 - int(target["truncation_pct"]) / 100.0
        return [Rule(service, "partial", 1.0, "*", {**tag, "keep": keep})]
    if cls == "env.pagination_change":
        return [Rule(service, "pagination", 1.0, "*",
                     {**tag, "page_size": int(target["page_size"]),
                      "cursor_format": str(target["cursor_format"])})]  # fmt: skip
    raise OverlayError(f"{cls} is not an environment class: it is delivered as a patch")


def _rule_json(rule: Rule) -> dict[str, Any]:
    return {"service": rule.service, "fault": rule.fault, "rate": rule.rate, "path": rule.path,
            "params": dict(rule.params)}  # fmt: skip


def _rule(data: Mapping[str, Any]) -> Rule:
    return Rule(str(data["service"]), data["fault"], float(data["rate"]), str(data["path"]),
                dict(data.get("params") or {}))  # fmt: skip


class ChaosOverlay:
    """Active environment sabotage, one JSON file per sealed spec, read by the chaos proxy."""

    def __init__(self, folder: Path, external: Mapping[str, Mapping[str, str]] | None = None,
                 ) -> None:  # fmt: skip
        self.folder = Path(folder)
        self.external = {h: dict(t) for h, t in (external or {}).items()}
        self._lock = threading.Lock()
        self._cache: tuple[float, list[tuple[str, Rule, datetime, datetime]]] | None = None

    @classmethod
    def for_fleet(cls, state_dir: Path, repo: Path) -> ChaosOverlay:
        from kintsugi.fleet.hosts import load_hosts

        external = {host: {name: tool.external for name, tool in spec.tools.items()
                           if tool.external}
                    for host, spec in load_hosts(repo).items()}  # fmt: skip
        return cls(Path(state_dir) / OVERLAY_DIR, external)

    def deliver(self, spec: Mapping[str, Any], *, now: datetime | None = None) -> str:
        """Activate an opened environment spec; returns the overlay file's path."""
        rules = compile_env(spec, self.external)
        start = now or datetime.now(UTC)
        duration = float((spec.get("schedule") or {}).get("duration_s") or DEFAULT_DURATION_S)
        body = {"sabotage": str(spec["id"]), "class": spec["class"],
                "host": spec["target"]["host"], "start": start.isoformat(),
                "until": (start + timedelta(seconds=duration)).isoformat(),
                "rules": [_rule_json(r) for r in rules]}  # fmt: skip
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{spec['id']}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
        with self._lock:
            self._cache = None
        return path.as_posix()

    def _load(self) -> list[tuple[str, Rule, datetime, datetime]]:
        if not self.folder.is_dir():
            return []
        stamp = max((p.stat().st_mtime for p in self.folder.glob("*.json")), default=0.0)
        with self._lock:
            if self._cache is not None and self._cache[0] == stamp:
                return self._cache[1]
        found = []
        for path in sorted(self.folder.glob("*.json")):
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
                start = datetime.fromisoformat(body["start"])
                until = datetime.fromisoformat(body["until"])
                for i, raw in enumerate(body["rules"]):
                    found.append((f"{body['sabotage']}:{i}", _rule(raw), start, until))
            except (OSError, ValueError, KeyError):
                continue  # a half-written or foreign file never breaks the proxy
        with self._lock:
            self._cache = (stamp, found)
        return found

    def rules(self, now: datetime | None = None) -> list[tuple[str, Rule]]:
        """The overlay rules active at ``now``, each with its stable draw key."""
        at = now or datetime.now(UTC)
        return [(key, rule) for key, rule, start, until in self._load() if start <= at < until]

    def active(self, now: datetime | None = None) -> list[str]:
        return sorted({key.split(":")[0] for key, _ in self.rules(now)})
