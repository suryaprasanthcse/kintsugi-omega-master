"""The Saboteur (spec §5.7, §7.2, Appendix B): catalog faults, sealed before they are delivered.

1. **Specify**: a spec with the §7.2 fields, its class from :data:`CATALOG` (Appendix B).
2. **Realism**: :func:`realism_problems` rejects non-catalog classes, targets outside the fleet,
   patches touching protected paths or outside the target host, destructive operations, and
   framings that leak the mechanism, the ground truth or the honeytoken. A rubric then scores
   the spec, which must reach 0.7. :func:`deterministic_rubric` is the fallback and the default:
   it needs no model, so tests and dry runs spend no LLM quota.
3. **Seal**: ``c = SHA-256(JCS(spec) ‖ salt)`` (``kintsugi.proof.sealed``); the public record
   ``{id, c, sealed_at, not_before, class_family}`` goes to ``sabotage-log/<id>.json`` (through a
   notary session inside W). The spec, salt and patch are age-encrypted into a store **outside
   every repository**; only the Saboteur activity holds the identity (A-57: S-17's patch is never
   written in clear into the fleet before delivery).
4. **Deliver**: a notary session applies the patch under the spec's own framing (A-52); the
   prompt never carries the mechanism, the ground truth, the honeytoken or the sabotage id.
5. **Reveal**: ``sabotage-log/<id>.reveal.json = {spec, salt}`` (audit C6).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyrage

from kintsugi.crew.notary import NotaryRequest
from kintsugi.gates.holdout import _inside_git_worktree
from kintsugi.gates.protected_paths import Policy, load_policy
from kintsugi.proof import sealed
from kintsugi.proof.canonical import canonicalize
from kintsugi.proof.passphrase import new_honeytoken

# Appendix B: class → its parameters.
CATALOG: dict[str, tuple[str, ...]] = {
    "env.rate_limit": ("tool", "rate", "burst"),
    "env.latency": ("tool", "p50_ms", "p95_ms"),
    "env.schema_drift": ("tool", "change"),
    "env.partial_response": ("tool", "truncation_pct"),
    "env.pagination_change": ("tool", "page_size", "cursor_format"),
    "code.regression.state_reset_on_retry": ("symbol",),
    "code.regression.off_by_one_pagination": ("symbol",),
    "code.regression.exception_swallow": ("symbol",),
    "model.swap_tier": ("step", "from", "to"),
    "prompt.edit_drop_constraint": ("prompt_id", "constraint"),
    "data.multicurrency_edge": ("currency_pairs",),
    "data.prompt_injection_in_document": ("field", "payload_class"),
    "healer.kill_worker": ("pool",),
    "healer.webhook_outage": ("duration_s",),
    "healer.neatlogs_ingest_delay": ("delay_s",),
    "healer.entire_hook_failure": ("repo",),
    "upstream.benign_drift": ("repo", "change"),
}
PATCH_CLASSES = ("code.", "prompt.", "upstream.")  # delivered as a patch in a session
DELIVERY_MODES = ("claude_code_session", "chaos_proxy", "chaos_api")
RUBRIC_THRESHOLD = 0.7
MAX_PATCH_LINES = 120
_DESTRUCTIVE = re.compile(
    r"rm\s+-rf|shutil\.rmtree|os\.remove|\.unlink\(|push\s+--force|reset\s+--hard|"
    r"filter-branch|drop\s+table|truncate\s+table|\.env\b|secret|api[_-]?key|token",
    re.IGNORECASE,
)
_WORDS = re.compile(r"sabotag|saboteur|honeytoken|sealed|injected fault", re.IGNORECASE)

Rubric = Callable[[Mapping[str, Any], str | None], float]


class SabotageRefused(ValueError):
    pass


def patch_sha256(patch: str) -> str:
    return "sha256:" + hashlib.sha256(patch.encode("utf-8")).hexdigest()


def patch_paths(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        if line.startswith(("+++ ", "--- ")):
            name = line[4:].strip().split("\t")[0]
            if name != "/dev/null":
                paths.append(name[2:] if name[:2] in ("a/", "b/") else name)
    return sorted(set(paths))


def leaks(text: str, spec: Mapping[str, Any]) -> list[str]:
    """What a delivery prompt must never carry (§7.2 step 5; A-52)."""
    found = []
    truth = dict(spec.get("ground_truth") or {})
    if spec.get("honeytoken") and str(spec["honeytoken"]) in text:
        found.append("the honeytoken")
    if truth.get("mechanism") and str(truth["mechanism"]) in text:
        found.append("the mechanism")
    if str(spec.get("id", "")) and re.search(rf"\b{re.escape(str(spec['id']))}\b", text):
        found.append("the sabotage id")
    if _WORDS.search(text):
        found.append("words that name the sabotage")
    return found


def realism_problems(
    spec: Mapping[str, Any],
    patch: str | None,
    *,
    fleet: Iterable[str],
    policy: Policy | None = None,
) -> list[str]:
    """The deterministic rule set (§7.2 step 2)."""
    try:
        sealed.check_spec_shape(spec)
    except ValueError as error:
        return [str(error)]
    problems: list[str] = []
    cls = str(spec["class"])
    host = str((spec.get("target") or {}).get("host", ""))
    delivery = dict(spec.get("delivery") or {})
    if cls not in CATALOG:
        problems.append(f"{cls!r} is not in the fault catalog (Appendix B)")
    if host not in set(fleet):
        problems.append(f"target {host!r} is outside the fleet: blast radius")
    if delivery.get("mode") not in DELIVERY_MODES:
        problems.append(f"unknown delivery mode {delivery.get('mode')!r}")
    if not isinstance(spec.get("seed"), int):
        problems.append("the seed must be an integer")
    if not str(spec.get("honeytoken", "")).strip():
        problems.append("a honeytoken is required (C6)")
    if not (spec.get("schedule") or {}).get("not_before"):
        problems.append("schedule.not_before is required")
    framing = str(delivery.get("framing", ""))
    problems += [f"the framing leaks {what}" for what in leaks(framing, spec)]
    if cls.startswith(PATCH_CLASSES):
        if patch is None:
            return [*problems, f"{cls} is delivered as a patch, and there is none"]
        if delivery.get("patch_sha256") != patch_sha256(patch):
            problems.append("delivery.patch_sha256 does not match the patch")
        paths = patch_paths(patch)
        outside = [p for p in paths if not p.startswith(f"hosts/{host}/")]
        if outside:
            problems.append(f"the patch reaches outside hosts/{host}/: {outside}")
        bad = (policy or load_policy()).violations(paths, ["surgeon"])
        if bad:
            problems.append(f"the patch touches protected paths: {sorted(v.path for v in bad)}")
        added = [ln[1:] for ln in patch.splitlines() if ln.startswith("+") and
                 not ln.startswith("+++")]  # fmt: skip
        if any(_DESTRUCTIVE.search(line) for line in added):
            problems.append("the patch adds a destructive or secret-touching operation")
        if str(spec.get("honeytoken")) in patch:
            problems.append("the patch carries the honeytoken")
    return problems


def deterministic_rubric(spec: Mapping[str, Any], patch: str | None) -> float:
    """Realism without a model: a plausible upstream change that hits its own ground truth."""
    score = 1.0
    delivery = dict(spec.get("delivery") or {})
    truth = dict(spec.get("ground_truth") or {})
    if not str(delivery.get("framing", "")).strip():
        score -= 0.3
    if not str(truth.get("mechanism", "")).strip():
        score -= 0.2
    if patch is not None:
        changed = [ln for ln in patch.splitlines()
                   if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]  # fmt: skip
        if len(changed) > MAX_PATCH_LINES:
            score -= 0.2
        if not set(truth.get("files") or ()) <= set(patch_paths(patch)):
            score -= 0.4
        symbols = [s.split(".")[0] for s in truth.get("symbols") or ()]
        if symbols and not any(s in patch for s in symbols):
            score -= 0.1
    return round(max(score, 0.0), 3)


def compose(
    sid: str,
    cls: str,
    *,
    host: str,
    framing: str,
    ground_truth: Mapping[str, Any],
    not_before: str,
    seed: int,
    patch: str | None = None,
    params: Mapping[str, Any] | None = None,
    jitter_s: int = 0,
    duration_s: float | None = None,
) -> dict[str, Any]:
    """A §7.2 spec with a fresh passphrase honeytoken (N1). ``duration_s``: how long an
    environment fault stays active once delivered (``chaos_overlay``)."""
    schedule: dict[str, Any] = {"not_before": not_before, "jitter_s": jitter_s}
    if duration_s is not None:
        schedule["duration_s"] = duration_s
    mode = ("claude_code_session" if cls.startswith(PATCH_CLASSES)
            else "chaos_api" if cls.startswith("healer.") else "chaos_proxy")  # fmt: skip
    delivery: dict[str, Any] = {"mode": mode, "framing": framing}
    if patch is not None:
        delivery["patch_sha256"] = patch_sha256(patch)
    return {
        "id": sid, "class": cls, "target": {"host": host, **dict(params or {})},
        "delivery": delivery, "ground_truth": dict(ground_truth),
        "schedule": schedule,
        "honeytoken": new_honeytoken(), "seed": seed,
    }  # fmt: skip


def new_file_diff(path: str, text: str) -> str:
    lines = text.rstrip("\n").split("\n")
    body = "".join(f"+{line}\n" for line in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}")  # fmt: skip


@dataclass
class Saboteur:
    """Seals into ``store`` (outside every repository) for the age ``recipients``."""

    store: Path
    recipients: tuple[str, ...]
    fleet: tuple[str, ...]
    rubric: Rubric = deterministic_rubric
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        self.store = Path(self.store)
        if _inside_git_worktree(self.store / "x"):
            raise SabotageRefused(f"{self.store} is inside a git worktree: specs stay outside")
        if not self.recipients:
            raise SabotageRefused("at least one age recipient is required")

    def _paths(self, sid: str) -> tuple[Path, Path, Path]:
        return (self.store / f"{sid}.age", self.store / f"{sid}.patch.age",
                self.store / f"{sid}.json")  # fmt: skip

    def record(self, sid: str) -> dict[str, Any] | None:
        public = self._paths(sid)[2]
        return json.loads(public.read_text(encoding="utf-8")) if public.is_file() else None

    def seal(self, spec: Mapping[str, Any], patch: str | None = None) -> dict[str, Any]:
        """Check, score, seal and store; returns the public commitment record. Sealing the
        same id again returns the existing record (an activity retry never re-seals)."""
        sid = str(spec.get("id"))
        existing = self.record(sid)
        if existing is not None:
            return existing
        problems = realism_problems(spec, patch, fleet=self.fleet)
        if problems:
            raise SabotageRefused("; ".join(problems))
        score = self.rubric(spec, patch)
        if score < RUBRIC_THRESHOLD:
            raise SabotageRefused(f"realism rubric {score:.2f} < {RUBRIC_THRESHOLD}")
        seal = sealed.seal(spec)
        now = self.clock().replace(microsecond=0).isoformat().replace("+00:00", "Z")
        not_before = str(spec["schedule"]["not_before"])
        record = sealed.commitment_record(spec, seal, sealed_at=now, not_before=not_before)
        spec_age, patch_age, public = self._paths(sid)
        self.store.mkdir(parents=True, exist_ok=True)
        spec_age.write_bytes(sealed.encrypt_sealed(spec, seal, self.recipients))
        if patch is not None:
            keys = [pyrage.x25519.Recipient.from_str(r) for r in self.recipients]
            patch_age.write_bytes(pyrage.encrypt(patch.encode("utf-8"), keys))
        public.write_bytes(canonicalize(record) + b"\n")
        return record

    def open(self, sid: str, identity: str) -> tuple[dict[str, Any], sealed.Seal, str | None]:
        spec_age, patch_age, _public = self._paths(sid)
        spec, seal = sealed.decrypt_sealed(spec_age.read_bytes(), identity)
        record = self.record(sid) or {}
        if record.get("c") != seal.commitment:
            raise SabotageRefused(f"{sid}: the stored spec no longer opens its commitment")
        patch = None
        if patch_age.is_file():
            key = pyrage.x25519.Identity.from_str(identity)
            patch = pyrage.decrypt(patch_age.read_bytes(), [key]).decode("utf-8")
            if spec["delivery"].get("patch_sha256") != patch_sha256(patch):
                raise SabotageRefused(f"{sid}: the stored patch does not match its hash")
        return spec, seal, patch

    @staticmethod
    def commitment_request(record: Mapping[str, Any]) -> NotaryRequest:
        """The notary request that publishes ``sabotage-log/<id>.json`` (inside W)."""
        path = f"sabotage-log/{record['id']}.json"
        return NotaryRequest(
            operation="seal-commitment", reference=path, incident=f"SEAL-{record['id']}",
            purpose="Publish this commitment record exactly as given.",
            diff=new_file_diff(path, canonicalize(dict(record)).decode("utf-8")),
            subject=f"sabotage-log: commitment {record['id']}",
            scopes=("surgeon", "eval_augment"),
        )  # fmt: skip

    @staticmethod
    def delivery_request(spec: Mapping[str, Any], patch: str) -> NotaryRequest:
        """The upstream change, framed by the spec's own framing (A-52), nothing more."""
        host = str(spec["target"]["host"])
        framing = str(spec["delivery"]["framing"]).rstrip(".")
        request = NotaryRequest(
            operation="upstream-change", reference=host, incident=f"UPSTREAM-{host}",
            purpose=f"{framing}. Apply this patch and run the existing tests.",
            diff=patch, subject=f"{host}: {framing[:80].lower()}",
            scopes=("surgeon",),
        )  # fmt: skip
        found = leaks(" ".join([request.purpose, request.subject, request.reference,
                                request.incident, request.diff]), spec)  # fmt: skip
        if found:
            raise SabotageRefused(f"the delivery would leak {found}")
        return request

    @staticmethod
    def reveal(spec: Mapping[str, Any], seal: sealed.Seal) -> tuple[str, bytes]:
        """``sabotage-log/<id>.reveal.json`` and its canonical bytes (§7.2 step 6)."""
        return (f"sabotage-log/{spec['id']}.reveal.json",
                canonicalize(sealed.reveal_record(spec, seal)) + b"\n")  # fmt: skip
