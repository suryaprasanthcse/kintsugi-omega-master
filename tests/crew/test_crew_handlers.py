"""Every activity the control plane declares has a real crew handler (Stage 5's ``Crew`` port)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kintsugi.control import contracts as c
from kintsugi.control.activities import SPECS, StepContext
from kintsugi.crew.factory import OmegaCrew
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.onboard.desk import OnboardingCase, OnboardingDesk

REPO = Path(__file__).resolve().parents[2]
CRM = REPO / "tests" / "fixtures" / "rehearsal" / "crm_practice"
FORM = (REPO / "tests" / "fixtures" / "rehearsal" / "oss1" / "intake.md").read_text(
    encoding="utf-8")  # fmt: skip


def test_all_declared_activities_have_a_handler() -> None:
    missing = sorted(name for name in SPECS if not callable(getattr(OmegaCrew, f"do_{name}", None)))
    assert missing == []
    assert len(SPECS) == 38


def _ctx(name: str) -> StepContext:
    return StepContext(name, f"{name}:key", 1, lambda *details: None)


def test_onboard_step_runs_the_desk_and_answers_without_one(tmp_path: Path) -> None:
    desk = OnboardingDesk(tmp_path / "cases", AntibodyLibrary(REPO / "antibodies"))
    desk.register(OnboardingCase("crm-practice", FORM, str(CRM), package_dir="practice_crm"))
    crew = SimpleNamespace(onboarding=desk)
    first = OmegaCrew.do_onboard_step(crew, _ctx("onboard_step"), "crm-practice", "O1")  # type: ignore[arg-type]
    assert isinstance(first, c.StepResult)
    assert first.ok and first.detail["pr_only"] is True  # noqa: PT018
    second = OmegaCrew.do_onboard_step(crew, _ctx("onboard_step"), "crm-practice", "O2")  # type: ignore[arg-type]
    assert second.ok and second.detail["manifests"] == ["pyproject.toml"]  # noqa: PT018
    bare = SimpleNamespace(onboarding=None)
    none = OmegaCrew.do_onboard_step(bare, _ctx("onboard_step"), "crm-practice", "O1")  # type: ignore[arg-type]
    assert not none.ok
    assert "no onboarding desk" in none.detail["error"]
