"""Typed messages between the control plane's workflows and activities (spec §5.3, §5.13).

Plain frozen dataclasses so Temporal's default converter serializes them deterministically, and
the SQLite-WAL reduced mode stores the same shapes as JSON. Nothing here does I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from kintsugi.control.states import TERMINAL, IncidentState

# The outcomes an Incident workflow can end with (I3: always one of these).
RESOLVED, ESCALATED, PARKED = (str(s) for s in (IncidentState.RESOLVED, IncidentState.ESCALATED,
                                                IncidentState.PARKED))  # fmt: skip


@dataclass(frozen=True)
class ControlConfig:
    """Bounds and pacing of the control plane, passed into workflows (never read from env)."""

    supervisor_id: str = "fleet-supervisor"
    max_rounds: int = 3  # repair rounds per pass (states.Bounds)
    max_canary_reentries: int = 2  # amendment A-5
    max_open_per_host: int = 2  # concurrency limit per host (§5.3.1)
    continue_as_new_every: int = 1000  # FleetSupervisor events per run (§5.3.1)
    canary_p0: float = 0.05  # the host's baseline failure rate (H0)
    canary_ratio: float = 2.0  # H1 = ratio × p0 (§5.12)
    canary_interval_s: float = 300.0  # durable timer between canary batches
    canary_max_batches: int = 48
    anchor_interval_s: float = 3600.0  # AuditAnchor cadence (hourly)
    heartbeat_s: float | None = None  # drills only: overrides every heartbeat timeout


@dataclass(frozen=True)
class Hit:
    """A detection hit as the senses deliver it (``kintsugi.senses.store.HitEvent``)."""

    hit_key: str
    detection_id: str
    trace_id: str
    span_id: str
    source: str = "poller"
    receptor: str | None = None
    host: str | None = None
    detected_at: str | None = None
    hit_id: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """The Sentinel's call for one hit: open a new incident or attach to an open one."""

    action: str  # "open" | "attach" | "drop"
    incident_id: str | None = None
    severity: str = "warning"
    reason: str = ""


@dataclass(frozen=True)
class Alert:
    incident_id: str
    host: str
    hits: tuple[Hit, ...]
    severity: str = "warning"
    supervisor_id: str | None = None
    reentries: int = 0
    evidence: tuple[str, ...] = ()  # canary evidence carried into a re-entry (§6.7)

    def with_evidence(self, *items: str) -> Alert:
        return replace(self, reentries=self.reentries + 1, evidence=(*self.evidence, *items))


@dataclass(frozen=True)
class Signature:
    incident_id: str
    host: str
    phi: str
    trace_ids: tuple[str, ...]
    severity: str = "warning"
    summary: str = ""
    pass_no: int = 0  # 0, then +1 for each canary re-entry: steps of a new pass are new work


@dataclass(frozen=True)
class AntibodyMatch:
    antibody_id: str
    version: str
    recipe_sha: str


@dataclass(frozen=True)
class Repro:
    ok: bool
    cassette_sha: str | None
    failures: int
    runs: int


@dataclass(frozen=True)
class Cause:
    suspects: tuple[str, ...]
    intent_record_sha: str | None = None
    report: str = ""


@dataclass(frozen=True)
class BudgetPlan:
    budget_usd: float
    k: int
    max_rounds: int
    tier: str
    plan_sha: str


@dataclass(frozen=True)
class Card:
    incident_id: str
    hypothesis: str  # "H1" … "Hk"
    round: int
    brief: str
    base_sha: str


@dataclass(frozen=True)
class SurgeonOutcome:
    incident_id: str
    hypothesis: str
    attempt: int
    branch: str
    nonce: str
    commit_sha: str | None
    checkpoint_id: str | None = None
    diff_sha: str | None = None
    adopted: bool = False  # attempt N found attempt N-1's commit and kept it (§6.6)
    abandoned: tuple[str, ...] = ()  # abandoned earlier attempts (no commit, no checkpoint)
    cost_usd: float = 0.0


@dataclass(frozen=True)
class GateVerdict:
    hypothesis: str
    passed: bool
    gates: dict[str, bool] = field(default_factory=dict)  # G0…G6
    diff_sha: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class Decision:
    round: int
    winner: str | None = None  # the winning hypothesis
    dispute: bool = False
    experiment_sha: str | None = None


@dataclass(frozen=True)
class Shipment:
    incident_id: str
    hypothesis: str
    branch: str
    pr_url: str | None
    g7_ok: bool
    merged: bool
    merge_sha: str | None = None


@dataclass(frozen=True)
class CanaryPlan:
    """H0's failure rate for the shipment's host and where it came from (§5.12)."""

    host: str
    p0: float
    ratio: float = 2.0
    source: str = ""


@dataclass(frozen=True)
class CanaryResult:
    accepted: bool
    rejected: bool
    samples: int
    llr: float
    trace_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AntibodyRef:
    antibody_id: str
    version: str
    state: str = ""  # its lifecycle state when the activity returned it (Stage 7)


@dataclass(frozen=True)
class TransitionRecord:
    incident_id: str
    seq: int  # position in the incident's transition log (REPAIRING → REPAIRING repeats)
    source: str
    target: str
    outcome: str


@dataclass(frozen=True)
class Outcome:
    incident_id: str
    state: str
    reason: str
    transitions: tuple[TransitionRecord, ...] = ()
    shipment: Shipment | None = None

    def __post_init__(self) -> None:
        if IncidentState(self.state) not in TERMINAL:
            raise ValueError(f"an incident ends in RESOLVED, ESCALATED or PARKED, not {self.state}")


@dataclass(frozen=True)
class BundleRef:
    """Where an incident's proof bundle was written and its digest (§7.4; the ledger's
    ``bundle_sha256``, E.2)."""

    incident_id: str
    path: str
    sha256: str


@dataclass(frozen=True)
class VaccinationResult:
    antibody_id: str
    host: str
    applied: bool
    reason: str = ""
    shipment: Shipment | None = None  # merged on our fleet (a canary follows), or a PR only


@dataclass(frozen=True)
class StepResult:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
