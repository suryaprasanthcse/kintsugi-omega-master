"""Onboarding O1–O7 in order, offline (Stage 10): intake → health card → replay → evals →
baseline → immune scan → the receipt's shipping terms. One record, every step's result."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kintsugi.control.ship import ShipConfig
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.onboard import eval_synth, immune_scan, instrument, intake, replay_mode


def ship_config(accepted: intake.Intake) -> ShipConfig:
    """O7: a foreign repository is pull-requests-only; Omega never merges there."""
    return ShipConfig(accepted.repo, pr_only=accepted.pr_only)


def onboard(
    form_body: str,
    checkout: Path,
    exports: Path | None,
    library: AntibodyLibrary,
    *,
    package_dir: str = ".",
    side_effect_tools: Iterable[str] = (),
) -> dict[str, Any]:
    accepted = intake.parse(form_body)  # O1: refuses without every consent
    card = instrument.scan(checkout)  # O2
    records = replay_mode.ExportedTraces(exports).records() if exports else []  # O3
    host = accepted.repo.split("/")[-1]
    shipping = ship_config(accepted)
    return {
        "O1_intake": {"repo": accepted.repo, "entry_point": accepted.entry_point,
                      "foreign": accepted.foreign, "consents": len(accepted.consents)},
        "O2_health_card": card.as_dict(),
        "O3_replay": {"traces": len(records),
                      "failures": len(replay_mode.observations(records, host))},
        "O4_evals": eval_synth.synthesize(records),
        "O5_baseline": eval_synth.baseline(records),
        "O6_immune_scan": immune_scan.scan(checkout, library, package_dir=package_dir,
                                           side_effect_tools=side_effect_tools),
        "O7_receipt": {"pr_only": shipping.pr_only, "merge": not shipping.pr_only,
                       "foreign_posts": "each one waits for the Architect's approval"},
    }  # fmt: skip
