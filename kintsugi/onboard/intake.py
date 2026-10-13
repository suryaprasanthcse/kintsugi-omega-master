"""O1: the Repair Clinic intake (Stage 10): an issue form, read strictly, consent required.

The form is ``.github/ISSUE_TEMPLATE/clinic-intake.yml``; GitHub renders its answers as
``### <label>`` sections and checkboxes as ``- [x] <text>``. An intake is accepted only with
every consent box checked; a repository other than Omega's own is foreign, so its receipt is a
pull request the maintainers merge (``pr_only``), never a merge by Omega.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONSENTS = (
    "I maintain this repository or have its maintainers' permission",
    "Omega may analyse a local copy of the repository read-only",
    "Fixes arrive as pull requests; only maintainers merge",
    "Traces are exported by us; Omega never collects them",
    "The repair receipt and its proof bundle may be published",
)
OWN = ("suryaprasanthcse/kintsugi-omega-master",)
_SLUG = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


class IntakeRefused(ValueError):
    pass


@dataclass(frozen=True)
class Intake:
    repo: str  # owner/name
    entry_point: str  # the agent's entry point, a path inside the repository
    description: str
    consents: tuple[str, ...]
    run_command: str | None = None  # how the maintainers run the agent (§5.9.4 O1)
    model_provider: str | None = None

    @property
    def foreign(self) -> bool:
        return self.repo not in OWN

    @property
    def pr_only(self) -> bool:
        return self.foreign


def sections(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    current = None
    for line in body.replace("\r\n", "\n").split("\n"):
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1)
            found[current] = ""
        elif current is not None:
            found[current] += line + "\n"
    return {k: v.strip() for k, v in found.items()}


def parse(body: str) -> Intake:
    found = sections(body)
    repo = found.get("Repository", "").strip().removeprefix("https://github.com/").strip("/")
    if not _SLUG.match(repo):
        raise IntakeRefused(f"repository {repo!r} is not owner/name")
    entry = found.get("Agent entry point", "").strip()
    if not entry or entry.startswith(("/", "\\")) or ".." in entry.split("/"):
        raise IntakeRefused("the agent entry point must be a path inside the repository")
    boxes = re.finditer(r"^- \[[xX]\]\s+(.+)$", found.get("Consent", ""), re.M)
    checked = tuple(m.group(1).strip() for m in boxes)
    missing = [c for c in CONSENTS if c not in checked]
    if missing:
        raise IntakeRefused(f"consent missing: {missing}")
    run = _optional(found.get("Run command", ""), 500)
    provider = _optional(found.get("Model provider", ""), 100)
    return Intake(repo, entry, found.get("What goes wrong", "")[:2000], checked,
                  run_command=run, model_provider=provider)  # fmt: skip


def _optional(value: str, limit: int) -> str | None:
    """An optional answer; GitHub renders an empty one as ``_No response_``."""
    text = value.strip().strip("`").strip()
    return None if not text or text == "_No response_" else text[:limit]
