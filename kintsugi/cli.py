"""`kintsugi` command-line entry point.

Every command is built. Exit codes: 0 ok, 1 failed; for `audit`, 2 is the spec's own meaning,
"checks skipped" (§7.5): a check without its inputs is SKIP, never PASS.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kintsugi import PINNED_TOOLCHAIN, __version__
from kintsugi.proof.attest import Ed25519Signer, fingerprint, load_public_key

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIPPED = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kintsugi",
        description="KINTSUGI-Ω: an immune system for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version and the pinned toolchain")

    audit = sub.add_parser("audit", help="verify the autonomy window (spec §7.5)")
    audit.add_argument("--all", action="store_true", help="run every check C1–C14")
    audit.add_argument("--autonomy", action="store_true", help="checks C2–C5 and C12")
    audit.add_argument("--incident", metavar="INC", help="one incident")
    audit.add_argument("--sabotage", metavar="S", help="one sealed sabotage")
    audit.add_argument("--antibody", metavar="AB", help="one antibody lineage")
    audit.add_argument("--offline", metavar="BUNDLE_DIR", help="verify published bundles only")
    audit.add_argument("--json", action="store_true", help="machine-readable output")

    t0 = sub.add_parser("t0", help="T0 preconditions (spec §6.1)")
    t0_sub = t0.add_subparsers(dest="t0_command", required=True)
    t0_check = t0_sub.add_parser("check", help="read-only preflight: what remains, and whose")
    t0_check.add_argument("--github", default="suryaprasanthcse/kintsugi-omega")
    t0_check.add_argument(
        "--rehearsal-identity", type=Path,
        default=Path.home() / ".kintsugi" / "keys" / "holdout-rehearsal.agekey",
    )  # fmt: skip
    t0_check.add_argument("--json", action="store_true")
    t0_gen = t0_sub.add_parser("genesis", help="at T0: window record + ledger genesis (A-67)")
    t0_gen.add_argument("--github", default="suryaprasanthcse/kintsugi-omega")
    t0_gen.add_argument("--ruleset", type=int, action="append", required=True,
                        help="id of a ruleset active on main (repeatable)")  # fmt: skip
    t0_gen.add_argument("--t0", help="RFC 3339 UTC (default: now)")

    demo = sub.add_parser("demo", help="replay a sealed sabotage and its audit (C1, C6) offline")
    demo.add_argument("--root", type=Path, default=None, help="scratch dir (default: temp)")
    demo.add_argument("--keep", action="store_true", help="keep the scratch practice repo")
    demo.add_argument("--json", action="store_true", help="the full summary as JSON")

    fleet = sub.add_parser("fleet", help="the host fleet: baselines, start, stop")
    fleet_sub = fleet.add_subparsers(dest="fleet_command", required=True)
    up = fleet_sub.add_parser("up", help="run the hosts: auto-deploy from main + live traffic")
    up.add_argument("--host", action="append", help="host id (default: every host)")
    up.add_argument("--seed", type=int, default=1)
    up.add_argument("--ref", default="origin/main", help="what the deploy worktree follows")
    up.add_argument("--no-deploy", action="store_true", help="run the installed code instead")
    up.add_argument("--scale", type=float, default=1.0, help="traffic speed-up (drills only)")
    up.add_argument("--duration", type=float, help="seconds to run (default: until fleet down)")
    fleet_sub.add_parser("down", help="ask a running `fleet up` to finish and exit")
    base = fleet_sub.add_parser("baseline", help="3-seed baseline and health card (spec §6.1)")
    base.add_argument("--host", action="append", help="host id (default: every host)")
    base.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    base.add_argument("--holdout-identity", type=Path, help="age identity file (outside repo)")
    base.add_argument("--workers", type=int, default=8)
    base.add_argument("--out-dir", type=Path, default=Path("evidence/baselines"))

    holdout = sub.add_parser("holdout", help="holdout custody (spec §5.11, I5, C13)")
    holdout_sub = holdout.add_subparsers(dest="holdout_command", required=True)
    keygen = holdout_sub.add_parser("keygen", help="new age identity; commits only the recipient")
    keygen.add_argument("--identity", type=Path, required=True, help="outside any repo")
    seal = holdout_sub.add_parser("seal", help="generate, canary and encrypt a host's holdout")
    seal.add_argument("--host", action="append", help="host id (default: every host)")
    holdout_sub.add_parser("verify", help="check sealed files against .kintsugi/holdout.sha256")

    senses = sub.add_parser("senses", help="neatlogs receptors, webhook ingress, poller (§4.1)")
    senses_sub = senses.add_subparsers(dest="senses_command", required=True)
    prov = senses_sub.add_parser("provision", help="receptors R-001…R-007, alert rules, webhook")
    prov.add_argument("--public-url", help="the ingress's public origin (e.g. a tunnel)")
    prov.add_argument("--dry-run", action="store_true", help="render and validate only")
    ingress = senses_sub.add_parser("ingress", help="serve the webhook ingress")
    ingress.add_argument("--port", type=int, default=8787)
    ingress.add_argument("--host", default="127.0.0.1")
    poll = senses_sub.add_parser("poll", help="the reconciliation poller (source of truth)")
    poll.add_argument("--once", action="store_true", help="one sweep of every receptor")
    poll.add_argument("--interval", type=float, default=60.0)
    complete = senses_sub.add_parser("completeness", help="invariant I7 over a window")
    complete.add_argument("--since", required=True, help="ISO-8601 start of the window")
    rehearse = senses_sub.add_parser("rehearse", help="R-007 on Ledger FAIL verdicts, end to end")
    rehearse.add_argument("--tasks", type=int, default=6)
    rehearse.add_argument("--seed", type=int, default=11)

    control = sub.add_parser("control", help="the control plane: supervisor, Watchdog (§5.3–5.5)")
    control_sub = control.add_subparsers(dest="control_command", required=True)
    commands = {
        "supervisor": "start the FleetSupervisor workflow (idempotent)",
        "forward": "forward queued senses hits to the FleetSupervisor",
        "watchdog": "supervise the worker pools; serve the chaos API",
        "history": "export a workflow's server-side event history (T8)",
    }
    for name, text in commands.items():
        cmd = control_sub.add_parser(name, help=text)
        cmd.add_argument("--target", default="localhost:7233", help="Temporal frontend")
    control_sub.choices["forward"].add_argument("--once", action="store_true")
    watch = control_sub.choices["watchdog"]
    watch.add_argument("--crew", required=True, help="module:factory of the Crew port")
    watch.add_argument("--chaos-port", type=int, default=8790)
    watch.add_argument("--ingress-admin", help="the ingress's /admin/availability URL")
    hist = control_sub.choices["history"]
    hist.add_argument("--workflow-id", required=True)
    hist.add_argument("--run-id")
    hist.add_argument("--out", type=Path, required=True)
    dead = control_sub.add_parser("deadman", help="page if the Watchdog's heartbeat is stale")
    dead.add_argument("--max-age", type=float, default=120.0)

    crew = sub.add_parser("crew", help="the crew and its rehearsals (spec §5.7, §6.2–6.5)")
    crew_sub = crew.add_subparsers(dest="crew_command", required=True)
    rehearse = crew_sub.add_parser(
        "rehearse", help="Stage 6 Ledger rehearsal on a disposable practice repository"
    )
    rehearse.add_argument("--run", default=None, help="run name (default: reh-<UTC time>)")
    rehearse.add_argument("--root", type=Path, default=Path.home() / ".kintsugi" / "rehearsals")
    rehearse.add_argument("--holdout-identity", type=Path, required=True)
    rehearse.add_argument("--github", default=None, help="owner/name: ship through PRs and G7")
    rehearse.add_argument("--crew-model", choices=["claude-cli", "offline"], default="claude-cli")
    rehearse.add_argument("--model", action="append", default=[], help="role=model override")
    rehearse.add_argument("--out", type=Path, default=None, help="evidence JSON (aggregates)")
    l3 = crew_sub.add_parser(
        "l3", help="Stage 7 rehearsal: antibody synthesis, maturation, vaccination, L3 drill"
    )
    l3.add_argument("--run", default=None, help="run name (default: l3-<UTC time>)")
    l3.add_argument("--root", type=Path, default=Path.home() / ".kintsugi" / "rehearsals")
    l3.add_argument("--holdout-identity", type=Path, required=True)
    l3.add_argument("--github", default=None, help="owner/name: pull requests and comments")
    l3.add_argument("--crew-model", choices=["claude-cli", "offline"], default="claude-cli")
    l3.add_argument("--out", type=Path, default=None, help="evidence JSON (aggregates)")

    gates = sub.add_parser("gates", help="verification gates (spec §5.11)")
    gates_sub = gates.add_subparsers(dest="gates_command", required=True)
    g7 = gates_sub.add_parser("g7", help="G7: G1–G4 with a fresh seed (GitHub Actions)")
    g7.add_argument("--repo", type=Path, required=True, help="the target repository checkout")
    g7.add_argument("--host", required=True)
    g7.add_argument("--base", required=True)
    g7.add_argument("--head", required=True)
    g7.add_argument("--seed", type=int, required=True)
    g7.add_argument("--tasks", default="", help="comma-separated repro task ids")
    g7.add_argument("--symptom", default="", help="the incident's error class (Kintsugi-Symptom)")
    g7.add_argument("--holdout-identity", type=Path, default=None)
    g7.add_argument("--out", type=Path, default=None, help="write the JSON summary here")

    econ = sub.add_parser("econ", help="the economic cortex: actuals, refit, cfo.ai (§4.6, L5)")
    econ_sub = econ.add_subparsers(dest="econ_command", required=True)
    actuals = econ_sub.add_parser("actuals", help="collect spend lines from evidence and state")
    actuals.add_argument("--write", action="store_true", help="update business/actuals.*")
    econ_sub.add_parser("refit", help="the nightly Wright learning-curve refit (prints JSON)")
    cfo = econ_sub.add_parser("cfo-export", help="the cfo.ai tabs as files (Ari MCP: A8)")
    cfo.add_argument("--out", type=Path, default=None, help="default business/cfo_export")

    onboard = sub.add_parser("onboard", help="Repair Clinic / OSS onboarding O1–O7 (§7.6)")
    onboard_sub = onboard.add_subparsers(dest="onboard_command", required=True)
    oss1 = onboard_sub.add_parser(
        "rehearse", help="offline OSS-1 rehearsal on the disclosed CRM practice host"
    )
    oss1.add_argument("--run", default=None, help="run name (default: oss1-<UTC time>)")
    oss1.add_argument("--root", type=Path, default=None, help="scratch dir (default: temp)")
    oss1.add_argument("--seed", type=int, default=1)
    oss1.add_argument("--limit", type=int, default=24, help="train tasks to run and export")
    oss1.add_argument("--out", type=Path, default=None, help="evidence JSON")
    onb = onboard_sub.add_parser("run", help="O1–O7 for an intake via the Onboarding workflow")
    onb.add_argument("--form", type=Path, required=True, help="the intake issue's body (markdown)")
    onb.add_argument("--checkout", type=Path, required=True, help="a local copy of the repository")
    onb.add_argument("--exports", type=Path, default=None, help="the maintainers' trace export")
    onb.add_argument("--package-dir", default=".")
    onb.add_argument("--side-effect-tool", action="append", default=[])
    onb.add_argument(
        "--root",
        type=Path,
        default=None,
        help="case folders (default: the runtime's <state>/onboarding, else temp)",
    )
    onb.add_argument("--runtime", type=Path, default=None, help="W runtime file (its desk)")
    onb.add_argument(
        "--temporal", default=None, help="W's Temporal frontend; default: an in-process test server"
    )

    arena = sub.add_parser("arena", help="the Arena board and public API (§7.7)")
    arena_sub = arena.add_subparsers(dest="arena_command", required=True)
    serve = arena_sub.add_parser(
        "serve", help="serve arena/ with GET /api/state and POST /api/sabotage"
    )
    serve.add_argument("--runtime", type=Path, default=None,
                       help="W runtime file (its sabotage store opens the Arena); default: "
                            "KINTSUGI_RUNTIME, else a closed, read-only board")  # fmt: skip
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--temporal", default="localhost:7233", help="Temporal frontend")
    serve.add_argument("--campaign", default=None, help="SabotageCampaign workflow id")
    serve.add_argument("--daily-cap", type=int, default=None)
    serve.add_argument(
        "--trust-forwarded",
        action="store_true",
        help="key clients by X-Forwarded-For (only behind your own proxy)",
    )

    keys = sub.add_parser("keys", help="attestation key custody (spec §4.5.1)")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    generate = keys_sub.add_parser(
        "generate", help="generate the Ed25519 attestation key; prints only the public half"
    )
    generate.add_argument("--private-key", type=Path, required=True, help="outside any repo")
    generate.add_argument("--public-key", type=Path, default=Path(".kintsugi/attest.pub"))
    return parser


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in path.resolve().parents)


def _keys_generate(private_key: Path, public_key: Path) -> int:
    if _inside_git_worktree(private_key):
        print(f"refusing: {private_key} is inside a git work tree; keep the key out of any repo")
        return EXIT_FAIL
    signer = Ed25519Signer.generate()
    try:
        signer.save(private_key, public_key)
    except FileExistsError as error:
        print(f"refusing: {error}")
        return EXIT_FAIL
    print(signer.public_key_openssh())
    print(f"fingerprint {fingerprint(load_public_key(public_key))}")
    print(f"public key written to {public_key}; private key kept at {private_key} (0600)")
    return EXIT_OK


def _repo() -> Path:
    return Path.cwd()


def _audit(args: argparse.Namespace) -> int:
    import json
    import os

    from kintsugi.fleet.fabric import load_dotenv
    from kintsugi.proof import audit

    repo = _repo()
    if args.autonomy:
        checks: tuple[str, ...] = ("C2", "C3", "C4", "C5", "C12")
    elif args.all or not (args.incident or args.sabotage or args.antibody):
        checks = audit.CHECKS
    else:
        checks = tuple(
            ({"C1", "C7", "C8", "C10"} if args.incident else set())
            | ({"C6"} if args.sabotage else set())
            | ({"C11"} if args.antibody else set())
        )
        checks = tuple(c for c in audit.CHECKS if c in checks)
    selection = audit.Selection(checks, args.incident, args.sabotage, args.antibody)
    offline = Path(args.offline) if args.offline else None
    online = audit.Online()
    window = audit.load_window(repo, offline)
    if offline is None and window is not None:
        env = {**load_dotenv(repo / ".env"), **os.environ}
        github = audit.github_ports(window, env.get("GITHUB_TOKEN"))
        senses = audit.neatlogs_ports(env, repo / ".state" / "senses" / "senses.sqlite")
        online = audit.Online(
            github.pr_checks, github.ruleset_history,
            senses.trace_started, senses.session_cost, senses.hits,
        )  # fmt: skip
    report = audit.run_audit(repo, offline=offline, selection=selection, online=online)
    print(json.dumps(report.as_json(), indent=2) if args.json else report.render())
    return report.exit_code


def _t0_check(args: argparse.Namespace) -> int:
    from kintsugi.control import preflight
    from kintsugi.control.ship import GhCli, ShipError
    from kintsugi.fleet.fabric import load_dotenv

    repo = _repo()
    if args.t0_command == "genesis":
        from kintsugi.control.genesis import GenesisError, write_genesis

        try:
            made = write_genesis(repo, github_repo=args.github, rulesets=args.ruleset, t0=args.t0)
        except GenesisError as error:
            print(f"t0 genesis refused: {error}")
            return EXIT_FAIL
        print(
            f"T0 {made.window.t0}  h_0 {made.genesis}\nseq 1 (genesis) {made.entry_hash}\n"
            f"next: commit {made.path.name} (the T0 commit), then push main and ledger"
        )
        return EXIT_OK
    gh_api = None
    with contextlib.suppress(ShipError):
        gh = GhCli(args.github, token=load_dotenv(repo / ".env").get("GITHUB_TOKEN"))
        gh_api = lambda path: gh.api("GET", path)  # noqa: E731
    items = preflight.check(repo, github_repo=args.github, gh_api=gh_api,
                            rehearsal_identity=args.rehearsal_identity)  # fmt: skip
    print(preflight.as_json(items) if args.json else preflight.render(items))
    return EXIT_OK if all(i.done for i in items) else EXIT_FAIL


def _onboard_run(args: argparse.Namespace) -> int:
    import json
    import tempfile

    from kintsugi.memory.library import AntibodyLibrary
    from kintsugi.onboard import pipeline
    from kintsugi.onboard.desk import OnboardingCase, OnboardingDesk
    from kintsugi.onboard.intake import IntakeRefused
    from kintsugi.onboard.workflow import record, run_onboarding

    form = args.form.read_text(encoding="utf-8")
    try:
        host = pipeline.host_id(form)
    except IntakeRefused as error:
        print(f"intake refused: {error}", file=sys.stderr)
        return EXIT_FAIL
    root = args.root
    if root is None and args.runtime is not None:
        from kintsugi.crew.runtime import RuntimeConfig

        root = RuntimeConfig.load(args.runtime).state_dir / "onboarding"  # W's desk
    root = root or Path(tempfile.mkdtemp(prefix="kintsugi-onboard-"))
    desk = OnboardingDesk(root, AntibodyLibrary(_repo() / "antibodies"))
    desk.register(OnboardingCase(host, form, str(args.checkout.resolve()),
                                 str(args.exports.resolve()) if args.exports else None,
                                 args.package_dir, tuple(args.side_effect_tool)))  # fmt: skip
    results = run_onboarding(desk, host, target=args.temporal)
    print(json.dumps({"host": host, "cases": str(root), **record(results)}, indent=1,
                     sort_keys=True, default=str))  # fmt: skip
    return EXIT_OK if len(results) == 7 and all(r.ok for r in results) else EXIT_FAIL


def _arena_config(args: argparse.Namespace) -> Any:
    import os

    from kintsugi.arena_service import CAMPAIGN_WORKFLOW_ID, ArenaConfig
    from kintsugi.crew.runtime import ENV, RuntimeConfig

    overrides: dict[str, Any] = {"temporal": args.temporal,
                                 "campaign_id": args.campaign or CAMPAIGN_WORKFLOW_ID}  # fmt: skip
    if args.daily_cap is not None:
        overrides["daily_cap"] = args.daily_cap
    runtime = args.runtime or (Path(os.environ[ENV]) if os.environ.get(ENV) else None)
    if runtime is not None:
        return ArenaConfig.from_runtime(RuntimeConfig.load(runtime), **overrides)
    return ArenaConfig(repo=_repo(), **overrides)


def _arena_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from kintsugi.arena_service import build_app

    cfg = _arena_config(args)
    app = build_app(cfg, trust_forwarded=args.trust_forwarded)
    state = "open (sealing into the runtime's store)" if cfg.open else "closed (board only)"
    print(f"arena: http://{args.host}:{args.port}/ — {state}; campaign {cfg.campaign_id} on "
          f"{cfg.temporal}")  # fmt: skip
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


def _crew_rehearse(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime

    from kintsugi.crew.rehearsal import Rehearsal, run
    from kintsugi.fleet.fabric import load_dotenv

    name = args.run or "reh-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    models = dict(item.split("=", 1) for item in args.model)
    token = load_dotenv(_repo() / ".env").get("GITHUB_TOKEN") if args.github else None
    cfg = Rehearsal(
        run=name,
        root=args.root / name,
        identity=args.holdout_identity,
        github=args.github,
        token=token,
        crew_model=args.crew_model,
        models=models,
        log=lambda line: print(line.encode("ascii", "replace").decode(), flush=True),
    )
    summary = run(cfg)
    out = args.out or _repo() / "evidence" / "rehearsals" / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(summary, indent=1, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"evidence written to {out}")
    return EXIT_OK if summary["outcome"]["state"] == "RESOLVED" else EXIT_FAIL


def _crew_l3(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime

    from kintsugi.crew.l3 import L3Rehearsal, run
    from kintsugi.fleet.fabric import load_dotenv

    name = args.run or "l3-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    token = load_dotenv(_repo() / ".env").get("GITHUB_TOKEN") if args.github else None
    cfg = L3Rehearsal(
        run=name,
        root=args.root / name,
        identity=args.holdout_identity,
        github=args.github,
        token=token,
        crew_model=args.crew_model,
        log=lambda line: print(line.encode("ascii", "replace").decode(), flush=True),
    )
    summary = run(cfg)
    out = args.out or _repo() / "evidence" / "rehearsals" / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, indent=1, sort_keys=True, default=str, ensure_ascii=False)
    out.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(f"evidence written to {out}")
    return EXIT_OK if summary["antibody"]["state"] == "ACTIVE" else EXIT_FAIL


def _gates_g7(args: argparse.Namespace) -> int:
    from kintsugi.gates.g7 import report, run_g7
    from kintsugi.gates.target import load_target

    target = load_target(args.repo.resolve(), args.host)
    identity = None
    if args.holdout_identity is not None:
        identity = args.holdout_identity.read_text(encoding="ascii").strip()
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    summary = run_g7(
        target,
        args.base,
        args.head,
        seed=args.seed,
        task_ids=tasks,
        identity=identity,
        symptom=args.symptom.strip() or None,
    )
    del identity
    text = report(summary)
    sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii"))  # cp1252 consoles
    if args.out is not None:
        args.out.write_text(text.splitlines()[-1] + "\n", encoding="utf-8", newline="\n")
    return EXIT_OK if summary["passed"] else EXIT_FAIL


def _holdout(args: argparse.Namespace) -> int:
    from kintsugi.fleet.hosts import load_hosts
    from kintsugi.gates import holdout

    repo = _repo()
    if args.holdout_command == "keygen":
        try:
            recipient = holdout.keygen(args.identity, repo)
        except holdout.HoldoutError as error:
            print(f"refusing: {error}")
            return EXIT_FAIL
        print(f"recipient {recipient} written to {holdout.RECIPIENT}; identity kept outside")
        return EXIT_OK
    if args.holdout_command == "seal":
        hosts = load_hosts(repo)
        for host_id in args.host or sorted(hosts):
            spec = hosts[host_id]
            report = holdout.seal(repo, spec.id, spec.package, spec.root)
            relative = report.path.resolve().relative_to(repo.resolve()).as_posix()
            print(
                f"{spec.id}: {report.tasks} tasks sealed in {relative} sha256:{report.sha256} "
                f"canary sha256:{report.canary_sha256}"
            )
        return EXIT_OK
    problems = holdout.verify(repo)
    for problem in problems:
        print(f"FAIL {problem}")
    print("holdout manifest: OK" if not problems else f"holdout manifest: {len(problems)} problems")
    return EXIT_FAIL if problems else EXIT_OK


def _fleet_baseline(args: argparse.Namespace) -> int:
    import json

    from kintsugi.fleet.baseline import baseline
    from kintsugi.fleet.fabric import FabricConfig, load_dotenv
    from kintsugi.fleet.hosts import load_hosts
    from kintsugi.gates import holdout

    repo = _repo()
    secrets = load_dotenv(repo / ".env")
    identity = None
    if args.holdout_identity:
        identity = args.holdout_identity.read_text(encoding="ascii").strip()
    neatlogs = {k: secrets[k] for k in ("NEATLOGS_API_KEY",) if k in secrets}
    hosts = load_hosts(repo)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    failed = False
    for host_id in args.host or sorted(hosts):
        spec = hosts[host_id]
        sealed = holdout.holdout_file(spec.package, spec.root)
        holdout_jsonl = None
        if identity and sealed.is_file():
            holdout_jsonl = holdout.tasks_jsonl(holdout.open_holdout(sealed, identity))
        config = FabricConfig(seed=0, anthropic_key=secrets.get("ANTHROPIC_API_KEY"))
        card = baseline(
            spec,
            args.seeds,
            holdout_jsonl=holdout_jsonl,
            config=config,
            workers=args.workers,
            extra_env=neatlogs,
        )
        out = args.out_dir / f"{spec.id}.json"
        out.write_text(json.dumps(card, indent=1) + "\n", encoding="utf-8", newline="\n")
        pooled = card["pooled"]
        print(
            f"{spec.id}: {pooled['passed']}/{pooled['n']} PASS "
            f"({pooled['pass_rate']:.3f}, Wilson95 {pooled['wilson95']}) "
            f"p95 {card['p95_latency_s']}s model={card['model_mode']} "
            f"splits={card['splits']} meets_slo={card['meets_slo']} -> {out}"
        )
        failed |= not card["meets_slo"]
    return EXIT_FAIL if failed else EXIT_OK


def _fleet_up(args: argparse.Namespace) -> int:
    from kintsugi.fleet.fabric import load_dotenv
    from kintsugi.fleet.hosts import load_hosts
    from kintsugi.fleet.runtime import STATE, Deployer, Runtime

    repo = _repo()
    secrets = load_dotenv(repo / ".env")
    hosts = load_hosts(repo)
    chosen = {h: hosts[h] for h in (args.host or sorted(hosts))}
    deployer = None if args.no_deploy else Deployer(repo, repo / STATE / "deploy", args.ref)
    neatlogs = {k: secrets[k] for k in ("NEATLOGS_API_KEY",) if k in secrets}
    key = secrets.get("ANTHROPIC_API_KEY")
    runtime = Runtime(
        repo,
        chosen,
        seed=args.seed,
        deployer=deployer,
        scale=args.scale,
        anthropic_key=key,
        extra_env=neatlogs,
    )
    print(
        f"fleet up: {sorted(chosen)} seed={args.seed} deploy={args.ref if deployer else 'off'}"
        " — stop with `kintsugi fleet down`"
    )
    with contextlib.suppress(KeyboardInterrupt):
        runtime.run(duration_s=args.duration)
    print(f"fleet down: live tasks run {runtime.counts}")
    return EXIT_OK


def _econ(args: argparse.Namespace) -> int:
    import json

    from kintsugi.econ import cfo_sync
    from kintsugi.econ.actuals import Actuals, collect

    repo = _repo()
    stored = repo / "business" / "actuals.json"
    if args.econ_command == "actuals":
        found = collect(repo)
        if args.write:
            stored.write_text(found.to_json(), encoding="utf-8", newline="\n")
            stored.with_suffix(".csv").write_text(found.to_csv(), encoding="utf-8", newline="\n")
        print(json.dumps({"spends": len(found.spends), "rehearsal": found.unit_costs(
            phase="rehearsal"), "live": found.unit_costs(phase="live")}, indent=1))  # fmt: skip
        return EXIT_OK
    if args.econ_command == "refit":
        print(json.dumps(cfo_sync.learning_curve(Actuals.load(stored)), indent=1))
        return EXIT_OK
    for path in cfo_sync.write_export(repo, Actuals.load(stored), args.out):
        print(f"wrote {path}")
    return EXIT_OK


def _fleet_down() -> int:
    from kintsugi.fleet.runtime import STOP_FILE

    stop = _repo() / STOP_FILE
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("stop\n", encoding="utf-8")
    print(f"fleet down: requested ({STOP_FILE})")
    return EXIT_OK


def _senses(args: argparse.Namespace) -> int:
    import threading
    from datetime import UTC, datetime

    from kintsugi.fleet.fabric import load_dotenv
    from kintsugi.senses import detections, service
    from kintsugi.senses.completeness import completeness, reported_hits
    from kintsugi.senses.store import SenseStore

    repo = _repo()
    if args.senses_command == "provision" and args.dry_run:
        rendered = detections.plan()
        for item in rendered.detections:
            count, window = item.alert.get("thresholdCount", 1), item.alert.get("windowMinutes", 0)
            print(
                f"neatlogs  {item.name:<22} {item.detection['type']:<10} "
                f"alert >={count} in {window}m"
            )
        for metric in rendered.metrics:
            print(
                f"omega     {metric.detection_id:<30} {metric.field} {metric.operator} "
                f"{metric.threshold:g}"
            )
        return EXIT_OK
    env = load_dotenv(repo / ".env")
    store = SenseStore(repo / service.STORE_PATH)
    if args.senses_command == "ingress":
        print(
            f"senses ingress: http://{args.host}:{args.port}/hook/<secret> (state "
            f"{service.STORE_PATH})"
        )
        service.serve(service.ingress_app(repo, store), host=args.host, port=args.port)
        return EXIT_OK
    with service.api_from_env(env) as api:
        if args.senses_command == "provision":
            manifest, changes = service.provision(repo, api, args.public_url)
            print(
                f"senses provision: {len(manifest.detections)} detections, "
                f"{len(manifest.metrics)} omega receptors, webhook "
                f"{'registered' if manifest.webhook_id else 'not registered (no --public-url)'}"
                f" -> {service.MANIFEST_PATH}"
            )
            for change in changes:
                print(f"  {change}")
            return EXIT_OK
        if args.senses_command == "poll":
            poll = service.poller(repo, api, store)
            if args.once:
                for report in poll.run_once():
                    print(
                        f"{report.detection_id}: {report.seen} seen, {report.new} new, "
                        f"{report.duplicates} already claimed, {report.pages} pages"
                    )
                return EXIT_OK
            print(f"senses poll: every {args.interval:g}s — Ctrl+C to stop")
            stop = threading.Event()
            with contextlib.suppress(KeyboardInterrupt):
                service.poll_forever(poll, stop, args.interval)
            return EXIT_OK
        if args.senses_command == "rehearse":
            from kintsugi.senses.rehearsal import rehearse

            manifest = service.load_manifest(repo)
            result = rehearse(
                repo,
                api,
                manifest,
                service.poller(repo, api, store),
                tasks=args.tasks,
                seed=args.seed,
            )
            new = sum(s.new for s in result.sweeps)
            print(
                f"rehearsal: {len(result.failed_tasks)}/{len(result.rows)} Ledger tasks FAIL "
                f"under the FX outage; R-007 hits on {result.hits} of them after "
                f"{result.waited_s:.0f}s; poller claimed {new} new hits "
                f"({store.pending()} queued)"
            )
            detected = bool(result.failed_tasks) and result.hits == len(result.failed_tasks)
            return EXIT_OK if detected else EXIT_FAIL
        start = datetime.fromisoformat(args.since.replace("Z", "+00:00")).astimezone(UTC)
        manifest = service.load_manifest(repo)
        hits = reported_hits(api, manifest.detections, start)
        report = completeness(hits, store.seen_hits(), start, datetime.now(UTC))
        print(f"I7 completeness since {start.isoformat()}: {report.summary()}")
        return EXIT_OK if report.ok else EXIT_FAIL


def _control(args: argparse.Namespace) -> int:
    import asyncio

    from kintsugi.control import contracts
    from kintsugi.control import service as svc
    from kintsugi.control import watchdog as w

    repo = _repo()
    if args.control_command == "deadman":
        pager = w.Pager(repo / svc.WATCHDOG_DIR / "pages.jsonl")
        alive = w.deadman(repo / svc.WATCHDOG_DIR / "heartbeat", args.max_age, pager)
        print("watchdog alive" if alive else "watchdog DEAD: paged")
        return EXIT_OK if alive else EXIT_FAIL
    if args.control_command == "history":
        n = asyncio.run(svc.export_history(args.target, args.workflow_id, args.out, args.run_id))
        print(f"{args.workflow_id}: {n} events -> {args.out}")
        return EXIT_OK
    if args.control_command == "supervisor":
        print(asyncio.run(svc.start_supervisor(args.target, contracts.ControlConfig())))
        return EXIT_OK
    if args.control_command == "forward":
        from kintsugi.senses.service import STORE_PATH

        moved = asyncio.run(
            svc.forward(
                args.target,
                repo / STORE_PATH,
                contracts.ControlConfig().supervisor_id,
                once=args.once,
            )
        )
        print(f"forwarded {moved} queued items")
        return EXIT_OK
    from kintsugi.senses.service import ingress_secrets

    admin = None
    if args.ingress_admin:
        admin = ingress_secrets(repo).admin_token
    dog = svc.build_watchdog(
        repo, args.target, args.crew, ingress_admin=args.ingress_admin, admin_token=admin
    )
    print(
        f"watchdog: pools {svc.summary(dog)}; chaos API on 127.0.0.1:{args.chaos_port}"
        f" (token in {svc.WATCHDOG_DIR / 'chaos.token'})"
    )
    svc.run_watchdog(dog, svc.chaos_token(repo), port=args.chaos_port)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "version":
        print(f"kintsugi {__version__}")
        for tool, version in PINNED_TOOLCHAIN.items():
            print(f"  {tool:<13} {version}")
        return EXIT_OK
    if args.command == "keys":
        return _keys_generate(args.private_key, args.public_key)
    if args.command == "holdout":
        return _holdout(args)
    if args.command == "senses":
        return _senses(args)
    if args.command == "control":
        return _control(args)
    if args.command == "gates":
        return _gates_g7(args)
    if args.command == "crew":
        return _crew_l3(args) if args.crew_command == "l3" else _crew_rehearse(args)
    if args.command == "fleet" and args.fleet_command == "baseline":
        return _fleet_baseline(args)
    if args.command == "fleet":
        return _fleet_up(args) if args.fleet_command == "up" else _fleet_down()
    if args.command == "audit":
        return _audit(args)
    if args.command == "econ":
        return _econ(args)
    if args.command == "arena":
        return _arena_serve(args)
    if args.command == "onboard" and args.onboard_command == "run":
        return _onboard_run(args)
    if args.command == "onboard":
        from kintsugi.onboard import rehearsal

        argv_oss1 = ["--seed", str(args.seed), "--limit", str(args.limit)]
        for flag, value in (("--run", args.run), ("--root", args.root), ("--out", args.out)):
            if value is not None:
                argv_oss1 += [flag, str(value)]
        return rehearsal.main(argv_oss1)
    if args.command == "t0":
        return _t0_check(args)
    if args.command == "demo":
        from kintsugi import demo

        return demo.main(root=args.root, keep=args.keep, as_json=args.json)
    raise SystemExit(f"kintsugi: unknown command {args.command!r}")  # argparse prevents this


if __name__ == "__main__":
    sys.exit(main())
