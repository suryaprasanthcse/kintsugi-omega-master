"""`kintsugi t0 check`: the read-only T0 preflight (spec §6.1, plan Stage 13 checklist).

It reads the repository, the remote and the local machine, changes nothing, and prints what is
done and which actions remain, and whose they are: the Architect's (custody, outward-facing
steps, the runtime outside the desktop app) or Omega's (code or records still missing). Items it
cannot observe from here are listed as Architect confirmations, never as done.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kintsugi.sandbox import proc

SABOTAGES = ("S-17", "S-18", "S-19")


@dataclass(frozen=True)
class Item:
    key: str
    done: bool
    owner: str  # "architect" | "omega"
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "done": self.done, "owner": self.owner, "detail": self.detail}


def _module_exists(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def _git(repo: Path, *args: str) -> proc.Result:
    return proc.run(["git", "-C", os.fspath(repo), *args], timeout_s=60)


def _hosts(repo: Path) -> list[str]:
    """The fleet's hosts (``fleet: false`` hosts such as Omega's replay suite have no baseline)."""
    from kintsugi.fleet.hosts import load_hosts

    return sorted(load_hosts(repo))


def _recipient_of(identity_file: Path) -> str | None:
    if not identity_file.is_file():
        return None
    import pyrage

    for line in identity_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("AGE-SECRET-KEY-"):
            return str(pyrage.x25519.Identity.from_str(line.strip()).to_public())
    return None


def check(
    repo: Path,
    *,
    github_repo: str,
    gh_api: Callable[[str], Any] | None,
    rehearsal_identity: Path,
) -> list[Item]:
    from kintsugi.gates import holdout

    repo = Path(repo)
    items: list[Item] = []
    hosts = [h for h in _hosts(repo) if (repo / "evidence" / "baselines" / f"{h}.json").is_file()]
    missing = sorted(set(_hosts(repo)) - set(hosts))
    items.append(Item("baselines", not missing, "omega",
                      f"3-seed health cards for {hosts}" + (f"; missing {missing}" if missing
                                                             else "")))  # fmt: skip
    problems = holdout.verify(repo)
    sealed_ok = "manifest matches every sealed file"
    items.append(Item("holdout-sealed", not problems, "omega", "; ".join(problems) or sealed_ok))
    pub = (repo / ".kintsugi" / "holdout.pub").read_text(encoding="utf-8").strip() \
        if (repo / ".kintsugi" / "holdout.pub").is_file() else ""  # fmt: skip
    rehearsal = _recipient_of(rehearsal_identity)
    resealed = bool(pub) and pub != rehearsal
    items.append(
        Item(
            "holdout-resealed",
            resealed,
            "architect",
            "holdout sealed to the T0 identity"
            if resealed
            else "re-seal the holdouts to an Architect-held identity (A-26: `kintsugi holdout "
            "keygen` outside the repo, then `kintsugi holdout seal`), commit the new "
            "recipient and manifest, and rotate the HOLDOUT_AGE_IDENTITY Actions secret",
        )
    )
    attest_pub = repo / ".kintsugi" / "attest.pub"
    items.append(
        Item(
            "attest-key",
            attest_pub.is_file(),
            "architect",
            "`.kintsugi/attest.pub` committed"
            if attest_pub.is_file()
            else "generate the T0 key outside any repo (`kintsugi keys generate "
            "--private-key <outside> --public-key .kintsugi/attest.pub`), commit the "
            "public key, post its fingerprint (A-4)",
        )
    )
    window = repo / ".kintsugi" / "window.json"
    genesis_tool = _module_exists("kintsugi.control.genesis")
    todo = ("" if genesis_tool else "no genesis command yet; ") + (
        "at T0: commit `.kintsugi/window.json` (A-67) with t0, base, attest_pubkey, "
        "holdout_manifest and rulesets, and append the genesis entry to `ledger`")  # fmt: skip
    items.append(Item("window-genesis", window.is_file(), "architect" if genesis_tool else "omega",
                      "window record committed" if window.is_file() else todo))  # fmt: skip
    remote_ledger = _git(repo, "ls-remote", "origin", "refs/heads/ledger")
    items.append(
        Item(
            "ledger-branch",
            bool(remote_ledger.stdout.strip()),
            "architect",
            "`ledger` is on origin"
            if remote_ledger.stdout.strip()
            else "push the `ledger` branch after the genesis entry (unprotected, D5)",
        )
    )
    sealed_ids = [s for s in SABOTAGES if (repo / "sabotage-log" / f"{s}.json").is_file()]
    saboteur = _module_exists("kintsugi.crew.saboteur")
    items.append(
        Item(
            "sabotage-sealed",
            len(sealed_ids) == len(SABOTAGES),
            "architect" if saboteur else "omega",
            ("" if saboteur else "the Saboteur is not built yet; ")
            + f"commitments committed: {sealed_ids or 'none'}; seal the rest after the "
            "go/no-go, before T0 (never write S-17 in clear into the fleet, A-57)",
        )
    )
    items.append(_rulesets(gh_api, github_repo))
    items += [
        Item(
            "background-chaos",
            False,
            "architect",
            "confirm `kintsugi fleet up` runs with the default background profile (fx_rate "
            "429s on 8%, disclosed) once the runtime is up",
        ),
        Item(
            "runtime",
            False,
            "architect",
            "start the runtime in WSL outside the desktop app (Temporal, worker, Watchdog, "
            "ingress, poller, fleet up) with keep-awake on",
        ),
        Item(
            "tunnel-webhook",
            False,
            "architect",
            "start cloudflared, then `kintsugi senses provision --public-url <tunnel>`: the "
            "webhook still points at a dead quick-tunnel URL",
        ),
    ]
    return items


def _rulesets(gh_api: Callable[[str], Any] | None, github_repo: str) -> Item:
    if gh_api is None:
        return Item("rulesets", False, "architect", "cannot read rulesets: gh is unavailable")
    try:
        listed = gh_api(f"repos/{github_repo}/rulesets") or []
        full = [gh_api(f"repos/{github_repo}/rulesets/{r['id']}") for r in listed]
    except Exception as error:
        return Item("rulesets", False, "architect", f"cannot read rulesets: {error}"[:200])
    on_main = [r for r in full if r and r.get("enforcement") == "active" and {
        "~DEFAULT_BRANCH", "refs/heads/main", "~ALL"} & set(
        ((r.get("conditions") or {}).get("ref_name") or {}).get("include") or [])]  # fmt: skip
    if on_main:
        ids = [r["id"] for r in on_main]
        return Item("rulesets", True, "architect", f"active on main: {ids}")
    return Item(
        "rulesets",
        False,
        "architect",
        "activate a ruleset on main (spec 4.5.3; today it protects only the Stage 6 "
        "rehearsal branches) and record its id in the window record",
    )


def render(items: Sequence[Item]) -> str:
    lines = [f"[{'x' if i.done else ' '}] {i.key:<17} {i.owner:<9} {i.detail}" for i in items]
    todo = [i for i in items if not i.done]
    lines.append(f"{len(items) - len(todo)}/{len(items)} done; remaining: "
                 f"{sum(i.owner == 'architect' for i in todo)} Architect, "
                 f"{sum(i.owner == 'omega' for i in todo)} Omega")  # fmt: skip
    return "\n".join(lines)


def as_json(items: Sequence[Item]) -> str:
    return json.dumps([i.as_dict() for i in items], indent=2)
