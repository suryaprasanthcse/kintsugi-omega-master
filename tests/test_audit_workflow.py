"""The audit workflow (spec §7.5, §12): C1–C14 offline on every push and daily; skipped checks
are reported, only a failing check fails the run; the demo must pass C1 and C6."""

from __future__ import annotations

from pathlib import Path

import yaml

from kintsugi.cli import _build_parser
from kintsugi.proof import audit

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "audit.yml"


def test_the_audit_workflow_runs_every_check_offline_and_the_demo() -> None:
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = flow.get("on", flow.get(True))  # YAML 1.1 reads a bare `on` as True
    assert set(triggers) == {"push", "schedule", "workflow_dispatch"}
    assert flow["permissions"] == {"contents": "read"}  # read-only: it verifies, never writes
    steps = flow["jobs"]["audit"]["steps"]
    run = next(s["run"] for s in steps if "kintsugi audit" in str(s.get("run", "")))
    assert "kintsugi audit --all --offline ./published-bundles --json" in run
    assert '[ "$code" -eq 1 ]' in run  # exit 1 fails the job; 2 (skipped) does not
    assert "report['checks']" in run  # the CLI's JSON shape
    demo = flow["jobs"]["demo"]["steps"][-1]["run"]
    assert demo.strip() == "uv run kintsugi demo"


def test_the_workflow_command_is_a_valid_cli_invocation() -> None:
    args = _build_parser().parse_args(["audit", "--all", "--offline", "./published-bundles",
                                       "--json"])  # fmt: skip
    assert (args.all, args.offline, args.json) == (True, "./published-bundles", True)
    assert len(audit.CHECKS) == 14
