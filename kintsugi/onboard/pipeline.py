"""Onboarding O1–O7 in order, in-process (Stage 10): the same desk the ``Onboarding`` workflow's
``onboard_step`` activities run (``kintsugi.onboard.desk``), for callers without a Temporal
server. One record, every step's result, keyed ``O<n>_<name>``."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kintsugi.control.ship import ShipConfig
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.onboard import intake
from kintsugi.onboard.desk import NAMES, STEPS, OnboardingCase, OnboardingDesk


def ship_config(accepted: intake.Intake) -> ShipConfig:
    """O7: a foreign repository is pull-requests-only; Omega never merges there."""
    return ShipConfig(accepted.repo, pr_only=accepted.pr_only)


def host_id(form_body: str) -> str:
    """The onboarded host's id: the repository's name, made safe for a case folder."""
    repo = intake.parse(form_body).repo
    return re.sub(r"[^A-Za-z0-9._-]", "-", repo.split("/")[-1])[:64] or "host"


def onboard(
    form_body: str,
    checkout: Path,
    exports: Path | None,
    library: AntibodyLibrary,
    *,
    package_dir: str = ".",
    side_effect_tools: Iterable[str] = (),
    root: Path | None = None,
) -> dict[str, Any]:
    """Run O1–O7; an intake without every consent is refused before anything else runs."""
    host = host_id(form_body)  # O1's parse: raises IntakeRefused
    desk = OnboardingDesk(Path(root or tempfile.mkdtemp(prefix="kintsugi-onboard-")), library)
    desk.register(OnboardingCase(host, form_body, str(checkout), str(exports) if exports else None,
                                 package_dir, tuple(side_effect_tools)))  # fmt: skip
    record: dict[str, Any] = {"host": host}
    for step in STEPS:
        result = desk.step(host, step)
        record[f"{step}_{NAMES[step]}"] = {"ok": result.ok, **result.detail}
        if not result.ok:
            break
    return record
