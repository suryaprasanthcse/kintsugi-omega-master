"""The Saboteur's healer port: ``healer.*`` faults through the Watchdog's chaos API (§6.6, App. B).

S-18 is ``healer.kill_worker(pool="surgeon-workers")`` together with
``healer.webhook_outage(duration_s=300)``, "both delivered through the Watchdog's chaos API".
One sealed spec carries both: its own class and parameters, plus ``target.together``, a list of
further healer actions (``{"class": "healer.webhook_outage", "duration_s": 300}``), delivered in
order under the same sabotage id.

- ``healer.kill_worker`` → ``POST /chaos/kill {pool, sabotage_id}``. Pool names follow the
  Watchdog's pools (``crew``, ``surgeon``); the spec's ``surgeon-workers`` form is accepted. The
  pool that runs the delivery itself (``crew``) is refused: it would kill the step mid-delivery.
- ``healer.webhook_outage`` → ``POST /chaos/ingress {available: false, duration_s, sabotage_id}``.
- ``healer.neatlogs_ingest_delay`` and ``healer.entire_hook_failure`` have no chaos endpoint:
  refused (non-retryable), never faked.

The bearer token is the Watchdog's own (``.state/watchdog/chaos.token``), read at delivery time
so the crew can be built before the Watchdog has written it. A 4xx answer is a refusal
(``DeliveryRefused``, not retried); a 5xx or a connection error is retried by the activity.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.control.activities import DeliveryRefused

TOKEN_FILE = Path(".state") / "watchdog" / "chaos.token"
DEFAULT_URL = "http://127.0.0.1:8790"
ENDPOINTLESS = ("healer.neatlogs_ingest_delay", "healer.entire_hook_failure")

# (url, json body, headers) → (status code, json answer). httpx by default; tests pass the app.
Post = Callable[[str, Mapping[str, Any], Mapping[str, str]], tuple[int, Any]]


def _httpx_post(url: str, body: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[int, Any]:
    import httpx

    response = httpx.post(url, json=dict(body), headers=dict(headers), timeout=30)
    try:
        answer = response.json()
    except ValueError:
        answer = {"text": response.text[:200]}
    return response.status_code, answer


def pool_name(pool: str) -> str:
    """``surgeon-workers`` (the spec's form) → ``surgeon`` (the Watchdog's pool)."""
    return pool.removesuffix("-workers")


@dataclass
class HealerPort:
    token_path: Path
    url: str = DEFAULT_URL
    own_pools: tuple[str, ...] = ("crew",)
    post: Post = field(default=_httpx_post)

    @classmethod
    def for_repo(cls, repo: Path, url: str = DEFAULT_URL) -> HealerPort:
        return cls(Path(repo) / TOKEN_FILE, url)

    def actions(self, spec: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        target = dict(spec.get("target") or {})
        together = target.pop("together", None) or []
        actions = [(str(spec["class"]), target)]
        for extra in together:
            params = dict(extra)
            actions.append((str(params.pop("class", "")), params))
        for cls, _ in actions:
            if not cls.startswith("healer."):
                raise DeliveryRefused(f"{spec.get('id')}: {cls!r} is not a healer class")
            if cls in ENDPOINTLESS:
                raise DeliveryRefused(f"{spec.get('id')}: no chaos endpoint delivers {cls}")
        return actions

    def deliver(self, spec: Mapping[str, Any]) -> str:
        """Every action of the spec, in order; returns the references joined by ``;``."""
        sid = str(spec.get("id") or "")
        if not sid:
            raise DeliveryRefused("a chaos call names its sealed sabotage: the spec has no id")
        actions = self.actions(spec)  # all refusals before the first call
        return ";".join(self._one(sid, cls, params) for cls, params in actions)

    def _token(self) -> str:
        if not self.token_path.is_file():
            raise RuntimeError(f"no chaos token at {self.token_path}: is the Watchdog running?")
        return self.token_path.read_text(encoding="utf-8").strip()

    def _call(self, path: str, body: Mapping[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self._token()}"}
        status, answer = self.post(f"{self.url.rstrip('/')}{path}", body, headers)
        if 400 <= status < 500:
            raise DeliveryRefused(f"chaos API {path} refused ({status}): {answer}")
        if status >= 300:
            raise RuntimeError(f"chaos API {path} failed ({status}): {answer}")
        return answer

    def _one(self, sid: str, cls: str, params: Mapping[str, Any]) -> str:
        if cls == "healer.kill_worker":
            pool = pool_name(str(params.get("pool", "")))
            if pool in self.own_pools:
                raise DeliveryRefused(f"{sid}: {pool} runs the delivery; it cannot be its target")
            answer = self._call("/chaos/kill", {"pool": pool, "sabotage_id": sid})
            return f"kill:{pool}:pid={answer.get('pid')}"
        if cls == "healer.webhook_outage":
            duration = float(params.get("duration_s", 0))
            if duration <= 0:
                raise DeliveryRefused(f"{sid}: a webhook outage needs a positive duration_s")
            self._call("/chaos/ingress", {"available": False, "duration_s": duration,
                                          "sabotage_id": sid})  # fmt: skip
            return f"ingress-outage:{duration:g}s"
        raise DeliveryRefused(f"{sid}: no chaos endpoint delivers {cls}")
