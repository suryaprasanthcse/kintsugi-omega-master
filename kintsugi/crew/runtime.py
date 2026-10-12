"""The W crew factory (Stage 13a step 1): ``kintsugi.crew.runtime:crew``.

``kintsugi control watchdog --crew kintsugi.crew.runtime:crew`` and every worker it supervises
build the crew here, from a runtime file named by ``KINTSUGI_RUNTIME`` (JSON, kept outside the
repository; it holds paths, never key material)::

    {"repo": "C:/…/kintsugi-omega-master", "github": "owner/name",
     "t0_key": "C:/Users/…/.kintsugi/keys/attest-t0", "crew_model": "claude-cli",
     "holdout_identity": "C:/Users/…/.kintsugi/keys/holdout.agekey",
     "sabotage": {"store": "C:/Users/…/.kintsugi/sabotage", "identity": "…/t0.agekey",
                  "recipient": "age1…"}}

What W changes against the rehearsals:

- **Ledger**: ``BranchLedger(repo, window.genesis())`` on the window record's ``ledger_ref``
  replaces the ``FileLedger``: every ``ledger_append`` is one commit on ``refs/heads/ledger``
  (A-67). The branch must already hold the ``genesis`` entry ``kintsugi t0 genesis`` wrote.
- **Signer**: the T0 Ed25519 key, loaded from outside every repository (A-4), and refused
  unless its public key is the window record's ``attest_pubkey``. Surgeons and notaries attest
  with it; ``AuditAnchor`` signs ``anchor-YYYYMMDD-HH`` with it hourly and the ledger branch is
  pushed fast-forward after each anchor; the post text goes to ``anchor_out`` for the Architect.
- **Sabotage lane**: the Saboteur's store and age identity (outside every repository), with the
  upstream lane (``kintsugi.crew.upstream``) as its ``deliver`` and ``publish`` ports.

``python -m kintsugi.crew.runtime check`` validates a runtime file and prints what is wired (no
secrets); ``start-anchors`` starts the ``AuditAnchor`` workflow at T0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kintsugi.control import contracts as c
from kintsugi.control.revert import RevertConfig, Reverter
from kintsugi.control.ship import GhCli, GitHubShipper, LocalShipper, ShipConfig
from kintsugi.crew.context import CrewContext, LocalTraces, NeatlogsTraces
from kintsugi.crew.factory import Anchoring, Immunity, OmegaCrew, SabotageLane
from kintsugi.crew.immunologist import Immunologist
from kintsugi.crew.notary import Notary
from kintsugi.crew.saboteur import Saboteur
from kintsugi.crew.surgeon import ClaudeSurgeon
from kintsugi.crew.upstream import UpstreamConfig, UpstreamLane
from kintsugi.econ.cortex import PLAN_FILE, Cortex
from kintsugi.gates.holdout import _inside_git_worktree
from kintsugi.gates.target import load_target, target_hosts
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.proof import attest
from kintsugi.proof.audit import WINDOW_FILE, Window
from kintsugi.proof.ledger_branch import BranchLedger

ENV = "KINTSUGI_RUNTIME"
ANCHOR_WORKFLOW_ID = "audit-anchor"


class RuntimeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SabotageConfig:
    store: Path
    identity: Path  # the age identity that opens the sealed specs (only the lane reads it)
    recipient: str  # the T0-held age recipient the specs are sealed to


@dataclass(frozen=True)
class RuntimeConfig:
    repo: Path
    t0_key: Path
    github: str | None = None  # owner/name; None ships locally (practice runs only)
    window: Path | None = None  # default: <repo>/.kintsugi/window.json
    state: Path | None = None  # default: <repo>/.state
    anchor_out: Path | None = None  # default: <state>/anchors
    remote: str = "origin"
    push_ledger: bool = True
    crew_model: str = "claude-cli"  # or "offline": deterministic fallbacks, no model calls
    models: Mapping[str, str] = field(default_factory=dict)  # role → model override
    holdout_identity: Path | None = None
    neatlogs: bool = False  # failing traces through the Public API (credentials from the env)
    upstream_checks: tuple[str, ...] = ()  # CI checks the upstream lane waits for
    sabotage: SabotageConfig | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RuntimeConfig:
        def path(key: str) -> Path | None:
            return Path(data[key]) if data.get(key) else None

        sab = data.get("sabotage")
        crew_model = str(data.get("crew_model", "claude-cli"))
        if crew_model not in ("claude-cli", "offline"):
            raise RuntimeConfigError(f"crew_model {crew_model!r}: claude-cli or offline")
        return cls(
            repo=Path(data["repo"]), t0_key=Path(data["t0_key"]), github=data.get("github"),
            window=path("window"), state=path("state"), anchor_out=path("anchor_out"),
            remote=str(data.get("remote", "origin")),
            push_ledger=bool(data.get("push_ledger", True)),
            crew_model=crew_model, models=dict(data.get("models") or {}),
            holdout_identity=path("holdout_identity"), neatlogs=bool(data.get("neatlogs", False)),
            upstream_checks=tuple(data.get("upstream_checks") or ()),
            sabotage=SabotageConfig(Path(sab["store"]), Path(sab["identity"]),
                                    str(sab["recipient"])) if sab else None,
        )  # fmt: skip

    @classmethod
    def load(cls, path: Path) -> RuntimeConfig:
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def state_dir(self) -> Path:
        return self.state or self.repo / ".state"

    @property
    def window_path(self) -> Path:
        return self.window or self.repo / WINDOW_FILE


def _outside(repo: Path, path: Path, what: str) -> None:
    if not path.is_file():
        raise RuntimeConfigError(f"{what} {path} does not exist")
    resolved = path.resolve()
    if resolved.is_relative_to(Path(repo).resolve()) or _inside_git_worktree(resolved):
        raise RuntimeConfigError(f"{what} {path} is inside a git worktree: keys stay outside (A-4)")


def load_window(cfg: RuntimeConfig) -> Window:
    if not cfg.window_path.is_file():
        raise RuntimeConfigError(
            f"{cfg.window_path} is missing: W starts with `kintsugi t0 genesis`"
        )
    window = Window.from_json(json.loads(cfg.window_path.read_text(encoding="utf-8")))
    if window.closed:
        raise RuntimeConfigError(f"W closed at {window.t1}: the crew does not run after W")
    return window


def load_signer(cfg: RuntimeConfig, window: Window) -> attest.Ed25519Signer:
    _outside(cfg.repo, cfg.t0_key, "the T0 key")
    signer = attest.Ed25519Signer.load(cfg.t0_key)
    raw = window.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if signer.public_key_raw() != raw:
        raise RuntimeConfigError("the T0 key is not the window record's attest_pubkey")
    return signer


def load_ledger(cfg: RuntimeConfig, window: Window) -> BranchLedger:
    ledger = BranchLedger(cfg.repo, window.genesis(), branch=window.ledger_ref)
    entries = ledger.entries()
    if not entries or entries[0].get("type") != "genesis":
        raise RuntimeConfigError(f"{ledger.ref} has no genesis entry: run `kintsugi t0 genesis`")
    return ledger


def build(cfg: RuntimeConfig) -> OmegaCrew:
    """The crew for W, refusing any configuration that would break a proof."""
    from kintsugi.crew.llm import ClaudeCliModel
    from kintsugi.senses.detections import Manifest
    from kintsugi.senses.service import MANIFEST_PATH, STORE_PATH, api_from_env
    from kintsugi.senses.store import SenseStore

    repo = Path(cfg.repo)
    window = load_window(cfg)
    signer = load_signer(cfg, window)
    ledger = load_ledger(cfg, window)
    if cfg.holdout_identity is not None:
        _outside(repo, cfg.holdout_identity, "the holdout identity")
    targets = {host: load_target(repo, host) for host in target_hosts(repo)}
    state = cfg.state_dir
    manifest = repo / MANIFEST_PATH
    live = cfg.crew_model == "claude-cli"
    ctx = CrewContext(
        state=state / "crew",
        targets=targets,
        model=ClaudeCliModel(overrides=dict(cfg.models)) if live else None,
        store=SenseStore(repo / STORE_PATH),
        bindings=Manifest.load(manifest).detections if manifest.is_file() else {},
        traces=(NeatlogsTraces(api_from_env(os.environ)) if cfg.neatlogs
                else LocalTraces(state / "observations")),
        holdout_identity=cfg.holdout_identity,
        models=dict(cfg.models),
    )  # fmt: skip

    def head() -> tuple[int, str]:
        entries = ledger.entries()
        return len(entries), str(entries[-1]["hash"])

    gh = GhCli(cfg.github) if cfg.github else None
    shipper: Any
    if gh is not None and cfg.github is not None:
        shipper = GitHubShipper(
            ctx, ShipConfig(cfg.github, remote=cfg.remote), gh, ledger_head=head
        )
    else:
        seed = int(datetime.now(UTC).timestamp()) % 100000
        shipper = LocalShipper(ctx, seed=seed, identity=cfg.holdout_identity, ledger_head=head)
    notary = Notary(signer)
    surgeon = ClaudeSurgeon(ctx, signer)
    shippers: dict[str, Any] = dict.fromkeys(targets, shipper)
    immunity = Immunity(
        Immunologist(ctx, AntibodyLibrary(repo / "antibodies"), ledger=ledger),
        shippers=shippers,
        reverters={
            host: Reverter(notary, RevertConfig(remote=cfg.remote), github=gh) for host in targets
        },
        notary=notary,
        github=gh,
    )
    repo_url = f"https://github.com/{cfg.github}" if cfg.github else repo.as_uri()
    anchoring = Anchoring(signer, repo_url, cfg.anchor_out or state / "anchors",
                          remote=cfg.remote if cfg.push_ledger else None)  # fmt: skip
    cortex = Cortex.from_repo(repo) if (repo / PLAN_FILE).is_file() else None  # Stage 9
    return OmegaCrew(ctx, runner=lambda: surgeon, shipper=shipper, ledger=ledger,
                     immunity=immunity, anchoring=anchoring, cortex=cortex,
                     sabotage=sabotage_lane(cfg, notary, gh, tuple(targets)))  # fmt: skip


def sabotage_lane(
    cfg: RuntimeConfig, notary: Notary, github: Any, fleet: tuple[str, ...]
) -> SabotageLane | None:
    sab = cfg.sabotage
    if sab is None:
        return None
    if github is None:
        raise RuntimeConfigError("the sabotage lane delivers through GitHub: set `github`")
    _outside(cfg.repo, sab.identity, "the sabotage identity")
    saboteur = Saboteur(sab.store, (sab.recipient,), fleet)
    upstream = UpstreamConfig(remote=cfg.remote, checks=cfg.upstream_checks)
    lane = UpstreamLane(cfg.repo, notary, github, upstream)
    identity = sab.identity.read_text(encoding="ascii").strip()
    return SabotageLane(saboteur, identity, deliver=lane.deliver, publish=lane.publish)


def crew() -> OmegaCrew:
    """The ``--crew`` factory: the runtime file named by ``KINTSUGI_RUNTIME``."""
    path = os.environ.get(ENV)
    if not path:
        raise RuntimeConfigError(f"{ENV} must name the runtime file (kept outside the repository)")
    return build(RuntimeConfig.load(Path(path)))


def describe(crew_: OmegaCrew) -> dict[str, Any]:
    """What is wired, for ``check`` (no key material, no secrets)."""
    ledger = crew_.ledger
    entries = ledger.entries()
    anchoring = crew_.anchoring
    return {
        "ledger": getattr(ledger, "ref", "file"),
        "seq": len(entries),
        "head": entries[-1]["hash"] if entries else None,
        "signer": attest.fingerprint(attest.load_public_key(anchoring.signer.public_key_openssh()))
        if anchoring else None,
        "anchors_pushed_to": anchoring.remote if anchoring else None,
        "hosts": sorted(crew_.ctx.targets),
        "shipper": type(crew_.shipper).__name__,
        "sabotage_lane": crew_.sabotage is not None,
        "model": "offline" if crew_.ctx.model is None else "claude-cli",
    }  # fmt: skip


def first_hour(now: datetime) -> int:
    return int(now.timestamp() // 3600)


async def start_anchors(
    target: str, config: c.ControlConfig, *, now: datetime | None = None
) -> str:
    """Start ``AuditAnchor`` (once, at T0): ``anchor_ledger`` every ``anchor_interval_s``."""
    from temporalio.client import Client
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from kintsugi.control.activities import CREW
    from kintsugi.control.workflows import AuditAnchor

    client = await Client.connect(target)
    hour = first_hour(now or datetime.now(UTC))
    try:
        handle = await client.start_workflow(AuditAnchor.run, args=[hour, config, 24],
                                             id=ANCHOR_WORKFLOW_ID, task_queue=CREW)  # fmt: skip
        return f"started {handle.id} at hour {hour}"
    except WorkflowAlreadyStartedError:
        return f"{ANCHOR_WORKFLOW_ID} is already running"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KINTSUGI-Omega W runtime (Stage 13a).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="validate the runtime file and print what is wired")
    check.add_argument("--runtime", type=Path, default=None)
    anchors = sub.add_parser("start-anchors", help="start the AuditAnchor workflow at T0")
    anchors.add_argument("--target", default="localhost:7233")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        if args.runtime is not None:
            os.environ[ENV] = str(args.runtime)
        try:
            print(json.dumps(describe(crew()), indent=1))
        except RuntimeConfigError as error:
            print(f"runtime: {error}", file=sys.stderr)
            return 1
        return 0
    print(asyncio.run(start_anchors(args.target, c.ControlConfig())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
