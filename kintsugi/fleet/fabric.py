"""The fabric a host runs behind: the LLM metering proxy and the seeded chaos proxy.

``with Fabric(hosts, FabricConfig(seed=2)) as fabric:`` starts both proxies on ephemeral ports
and ``fabric.env(host)`` gives a host process exactly what it needs to reach them, and nothing
secret: a ``kintsugi-proxy-`` token for the LLM proxy and the chaos proxy's URL. The model
upstream is the Anthropic API when a key is configured, otherwise each host's labelled offline
stand-in (amendment A-23). Spend is metered per host in the shared SQLite ledger.
"""

from __future__ import annotations

import inspect
import secrets
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.sandbox import llm_proxy as lp
from kintsugi.sandbox.cassette import Player, Recorder
from kintsugi.sandbox.chaos_overlay import ChaosOverlay
from kintsugi.sandbox.httpserve import Handler, HttpRequest, HttpResponse, Server
from kintsugi.sandbox.runner import PROXY_PLACEHOLDER

from .hosts import HostSpec, resolve

DAILY_CAP_USD = 20.0  # amendment A-23 (ruling D7)


def load_dotenv(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines of a ``.env`` file (values are never printed)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ServiceFn = Callable[..., tuple[int, Any]]


def service_handler(fn: ServiceFn) -> Handler:
    """Adapt a host's mock service ``(method, path, query, body[, namespace]) -> (status,
    json)``. A service that takes ``namespace`` gets the calling task's id, so stateful mocks
    (Triage's GitHub) keep each task's side effects apart."""
    namespaced = "namespace" in inspect.signature(fn).parameters

    def handle(request: HttpRequest) -> HttpResponse:
        args = (request.method, request.path, request.query, request.body)
        status, body = fn(*args, namespace=request.task) if namespaced else fn(*args)
        return HttpResponse.of_json(status, body)

    return handle


@dataclass
class FabricConfig:
    seed: int
    profile: chaos.Profile = field(default_factory=chaos.background_profile)
    daily_cap_usd: float = DAILY_CAP_USD
    spend_path: Path | str = ":memory:"
    anthropic_key: str | None = None
    bind: str = "127.0.0.1"
    chaos_recorder: Recorder | None = None
    llm_recorder: Recorder | None = None
    player: Player | None = None  # replay a cassette: tool and model I/O, faults included
    live_services: tuple[str, ...] = ()  # the host's own state: never recorded or replayed
    overlay: ChaosOverlay | None = None  # environment sabotage delivered at run time


class Fabric:
    def __init__(self, hosts: Mapping[str, HostSpec], config: FabricConfig) -> None:
        self.hosts = dict(hosts)
        self.config = config
        self.tokens = {h: f"{PROXY_PLACEHOLDER}{h}-{secrets.token_hex(8)}" for h in self.hosts}
        upstream: lp.Upstream
        if config.player is not None:
            upstream = lp.CassetteUpstream(config.player)
        elif config.anthropic_key:
            upstream = lp.AnthropicUpstream(config.anthropic_key)
        else:
            upstream = lp.OfflineUpstream(
                {h: resolve(spec.offline_model) for h, spec in self.hosts.items()}
            )
        self.upstream = upstream
        limits = {lp.account_of(h): lp.AccountLimits(spec.economics.llm_budget_usd_month)
                  for h, spec in self.hosts.items()}  # fmt: skip
        self.spend = lp.SpendLedger(config.spend_path, daily_cap_usd=config.daily_cap_usd,
                                    limits=limits)  # fmt: skip
        services: dict[str, chaos.Upstream] = {}
        self._resets: list[Callable[[], None]] = []
        for spec in self.hosts.values():
            for name, dotted in spec.services.items():
                if name in services:
                    raise ValueError(f"service {name!r} is declared by two hosts")
                fn = resolve(dotted)
                services[name] = service_handler(fn)
                reset: Callable[[], None] | None = getattr(inspect.getmodule(fn), "reset", None)
                if reset is not None:  # stateful mocks start empty for every fabric
                    self._resets.append(reset)
        profile = config.profile.with_seed(config.seed)
        self.chaos = chaos.ChaosProxy(
            services, profile, recorder=config.chaos_recorder, player=config.player,
            live=frozenset(config.live_services), overlay=config.overlay,
        )  # fmt: skip
        self.llm = lp.LlmProxy(upstream, self.spend, self.tokens, recorder=config.llm_recorder)
        self._servers = [Server(self.llm, host=config.bind), Server(self.chaos, host=config.bind)]

    @property
    def model_mode(self) -> str:
        return self.upstream.name

    def env(self, host: str) -> dict[str, str]:
        llm_server, chaos_server = self._servers
        return {
            "ANTHROPIC_BASE_URL": f"{llm_server.url}/h/{host}",
            "ANTHROPIC_API_KEY": self.tokens[host],
            "KINTSUGI_EXT_URL": chaos_server.url,
            "KINTSUGI_HOST": host,
            "KINTSUGI_SEED": str(self.config.seed),
        }

    def chaos_counts(self) -> Counter[str]:
        """Calls and injected faults per service, e.g. ``fx:calls``, ``fx:status#0``."""
        counts: Counter[str] = Counter()
        for row in self.chaos.log:
            counts[f"{row['service']}:calls"] += 1
            for fired in filter(None, str(row["fired"]).split(",")):
                if not fired.startswith("latency"):
                    counts[f"{row['service']}:{fired}"] += 1
        return counts

    def __enter__(self) -> Fabric:
        for reset in self._resets:
            reset()
        for server in self._servers:
            server.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for server in self._servers:
            server.stop()
        self.chaos.close()
