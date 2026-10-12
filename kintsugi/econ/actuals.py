"""Actuals: what Omega and the fleet actually spent, per host and month (Stage 9; spec §4.6.4–5).

Every line is a :class:`Spend` with its source, so a number in ``business/actuals.json`` can be
traced back to the record it came from:

- **Rehearsal evidence** (``evidence/rehearsals/*.json``): a Stage 6 rehearsal's crew cost for
  its incident (``kind=repair``) and a Stage 7 run's vaccination cases (``kind=vaccination``);
  the origin incident a Stage 7 run reuses is not counted twice. These lines carry
  ``phase=rehearsal`` and never enter a live month-to-date figure.
- **The LLM proxy's spend ledger** (``.state/runtime/spend.sqlite``, ``charges``): billable
  host model spend per account and day (``kind=llm``, ``phase=live``); the hosts' operating
  cost C_mon.
- **Omega's own live sessions** recorded by the crew (``kind`` from the case: ``repair``,
  ``vaccination``, ``treatment``), appended with :meth:`Actuals.add` as W runs.

``month_to_date`` is the governor's ``spent_mtd``; ``to_csv`` is the cfo.ai import format.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

KINDS = ("repair", "vaccination", "treatment", "backfill", "llm", "monitoring")
PHASES = ("live", "rehearsal")
FIELDS = ("date", "host", "kind", "usd", "ref", "source", "phase")
_RUN_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})-\d{6}")


class ActualsError(ValueError):
    pass


@dataclass(frozen=True)
class Spend:
    date: str  # YYYY-MM-DD (UTC)
    host: str
    kind: str
    usd: float
    ref: str  # the incident, case or account the spend belongs to
    source: str  # the record the number comes from
    phase: str = "live"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ActualsError(f"unknown spend kind {self.kind!r}")
        if self.phase not in PHASES:
            raise ActualsError(f"unknown phase {self.phase!r}")
        if self.usd < 0:
            raise ActualsError(f"{self.ref}: a spend is never negative")
        datetime.strptime(self.date, "%Y-%m-%d")  # must parse

    @property
    def month(self) -> str:
        return self.date[:7]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.ref, self.date)


def _run_date(run: str) -> str:
    found = _RUN_DATE.search(run)
    if not found:
        raise ActualsError(f"{run}: no YYYYMMDD-HHMMSS in the run name")
    return "-".join(found.groups())


def _vaccinated_host(case: str) -> str:
    """``VAC-AB-1-crm-practice`` → ``crm-practice``."""
    parts = case.split("-")
    return "-".join(parts[3:]) if len(parts) > 3 else case


def from_rehearsal(path: Path, root: Path | None = None) -> list[Spend]:
    """The spend lines one rehearsal summary records (aggregates only)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    source = Path(path).relative_to(root).as_posix() if root else Path(path).name
    run = str(data.get("run", Path(path).stem))
    costs = data.get("costs") or {}
    found: list[Spend] = []
    if "plan" in data and "total_usd" in costs:  # a Stage 6 incident rehearsal
        plan = data["plan"]
        found.append(Spend(_run_date(run), str(plan["host"]), "repair",
                           round(float(costs["total_usd"]), 6), str(plan["incident"]), source,
                           "rehearsal"))  # fmt: skip
    for case, usd in sorted((costs.get("vaccinations_usd") or {}).items()):
        found.append(Spend(_run_date(run), _vaccinated_host(case), "vaccination",
                           round(float(usd), 6), case, source, "rehearsal"))  # fmt: skip
    return found


def case_costs(path: Path) -> dict[str, float]:
    """Case-file spend per incident (``costs.cases``): what the governor's G6 check counts,
    so it takes precedence over a summary's per-role total for the same incident."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = (data.get("costs") or {}).get("cases") or {}
    return {str(k): round(float(v), 6) for k, v in cases.items() if str(k).startswith("INC-")}


def from_spend_ledger(path: Path) -> list[Spend]:
    """Billable host model spend per account and day from the LLM proxy's ``charges``."""
    if not Path(path).is_file():
        return []
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT account, day, SUM(cost_usd) FROM charges WHERE billable = 1 "
                          "GROUP BY account, day ORDER BY day, account").fetchall()  # fmt: skip
    return [Spend(str(day), str(account), "llm", round(float(usd), 6), f"proxy:{account}",
                  "spend.sqlite", "live") for account, day, usd in rows]  # fmt: skip


@dataclass(frozen=True)
class Actuals:
    spends: tuple[Spend, ...] = ()

    def add(self, *more: Spend) -> Actuals:
        """A new set with ``more`` appended; a line whose (kind, ref, date) exists is ignored."""
        merged = {s.key: s for s in self.spends}
        for line in more:
            merged.setdefault(line.key, line)
        order = sorted(merged.values(), key=lambda s: (s.date, s.host, s.kind, s.ref))
        return Actuals(tuple(order))

    def select(
        self,
        *,
        host: str | None = None,
        month: str | None = None,
        phase: str | None = "live",
        kinds: Iterable[str] | None = None,
    ) -> list[Spend]:
        wanted = set(kinds) if kinds is not None else None
        return [s for s in self.spends
                if (host is None or s.host == host) and (month is None or s.month == month)
                and (phase is None or s.phase == phase)
                and (wanted is None or s.kind in wanted)]  # fmt: skip

    def total(self, **where: Any) -> float:
        return round(sum(s.usd for s in self.select(**where)), 6)

    def month_to_date(self, host: str, month: str) -> float:
        """The governor's ``spent_mtd``: live repair-side spend (repairs, vaccinations,
        treatments, backfills) for the host this month. Rehearsals never count."""
        return self.total(host=host, month=month, kinds=("repair", "vaccination", "treatment",
                                                         "backfill"))  # fmt: skip

    def operating_cost(self, host: str, month: str) -> float:
        """C_mon actuals: the host's own model spend and monitoring this month."""
        return self.total(host=host, month=month, kinds=("llm", "monitoring"))

    def unit_costs(self, *, phase: str | None = None) -> dict[str, float | int]:
        """Mean primary and secondary response costs (§3.5.2's c_p and c_s)."""
        primary = [s.usd for s in self.select(phase=phase, kinds=("repair",))]
        secondary = [s.usd for s in self.select(phase=phase, kinds=("vaccination",))]
        return {
            "primary_n": len(primary),
            "primary_mean_usd": round(sum(primary) / len(primary), 6) if primary else 0.0,
            "secondary_n": len(secondary),
            "secondary_mean_usd": round(sum(secondary) / len(secondary), 6) if secondary else 0.0,
        }  # fmt: skip

    # ---- files --------------------------------------------------------------------------------

    def to_json(self) -> str:
        body = {"fields": list(FIELDS), "spends": [asdict(s) for s in self.spends]}
        return json.dumps(body, indent=1, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Actuals:
        data = json.loads(text)
        return cls().add(*(Spend(**row) for row in data.get("spends") or ()))

    @classmethod
    def load(cls, path: Path) -> Actuals:
        return cls.from_json(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() \
            else cls()  # fmt: skip

    def to_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(FIELDS)
        for s in self.spends:
            writer.writerow([s.date, s.host, s.kind, f"{s.usd:.6f}", s.ref, s.source, s.phase])
        return out.getvalue()


def collect(repo: Path, *, spend_ledger: Path | None = None,
            extra: Sequence[Spend] = ()) -> Actuals:  # fmt: skip
    """Every spend the repository and the runtime state record."""
    repo = Path(repo)
    found: list[Spend] = []
    cases: dict[str, tuple[float, str]] = {}
    for path in sorted((repo / "evidence" / "rehearsals").glob("*.json")):
        found += from_rehearsal(path, repo)
        source = path.relative_to(repo).as_posix()
        cases.update({ref: (usd, source) for ref, usd in case_costs(path).items()})
    found = [replace(s, usd=cases[s.ref][0], source=f"{s.source}; case cost: {cases[s.ref][1]}")
             if s.kind == "repair" and s.ref in cases else s for s in found]  # fmt: skip
    found += from_spend_ledger(spend_ledger or repo / ".state" / "runtime" / "spend.sqlite")
    return Actuals().add(*found, *extra)


def summary(actuals: Actuals, month: str, hosts: Sequence[str]) -> Mapping[str, Any]:
    return {host: {"spent_mtd": actuals.month_to_date(host, month),
                   "operating_cost": actuals.operating_cost(host, month)}
            for host in hosts}  # fmt: skip
