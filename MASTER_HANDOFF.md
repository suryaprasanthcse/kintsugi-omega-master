# KINTSUGI-Ω Master Edition — Handoff

| Field | Value |
|---|---|
| Written | 2026-10-12 (IST), when this repository was created |
| Architect | Surya Prasanth R (`@suryaprasanthcse`): sole human architect, sole contributor, sole commit author |
| This repository | https://github.com/suryaprasanthcse/kintsugi-omega-master · local `C:\myspace\hacckathons\kintsugi-omega-master` |
| Frozen submission | https://github.com/suryaprasanthcse/kintsugi-omega at `13a744b` (#neatHack). **Never** fetch into, commit to, push to, or add a remote for it. |
| Base | `13a744b` "chore: license the repository under MIT", omega-ci run 38259678672 green on the frozen repo |
| Status | Stages 1–8 built and verified. The autonomy window W did not run (NO-GO, 2026-10-12). **Updated 2026-10-13 (overnight run):** Stage 13a, Stage 9 and Stage 12 built and verified offline; Stage 10 and 11 offline modules built (A-69 … A-72, `docs/VERIFICATION.md`). Remaining: the Architect's items in §5, then 13b. |

## 1. Current state (inherited from `13a744b`)

- **Tests:** full suite **842 passed, 30 skipped** on `7a2fc99` (the last code commit; later commits are docs, the license and `pyproject.toml` metadata). The 30 skips are contract tests that need local tooling (`KINTSUGI_CONTRACT=1`).
- **CI:** `.github/workflows` run lint · typecheck · test and one G7 job per host (ledger, scout, triage). The only secret they read is `HOLDOUT_AGE_IDENTITY`.
- **Stages 1–8** (`docs/EXECUTION_PLAN.md`, results in `docs/VERIFICATION.md`): proof cores, the Entire and entire-graph layer, the fleet (Ledger, Scout, Triage), senses (neatlogs receptors, ingress, poller), the Temporal control plane, crew and gates G0–G7, antibodies with the canary and L3 quarantine, and the proof plane (`kintsugi audit` C1–C14 with a 33-test mutation suite, `BranchLedger` with signed `anchor-*` tags, the Saboteur, `kintsugi t0 check`, `kintsugi t0 genesis`).
- **Rehearsal evidence:** `evidence/rehearsals/l3-20261012-074523.json` and `antibodies/AB-1` (AB-1@1.1 ACTIVE). Primary repair $0.665, vaccinations $0.089 and $0.104, backfill $12.00, disclosed L3 drill (frozen repo PRs #7–#10). Rehearsal branches `rehearsal/*` are carried over as history.
- **Amendments:** A-1 … A-68 in `docs/EXECUTION_PLAN.md` at the base; this edition adds A-69 … A-72. The next one is **A-73**.
- **Not carried over:** `.env`, `.state/` and every key file stay outside git. No `ledger` branch and no `anchor-*` tags exist (no T0 ever happened). The frozen repo's GitHub rulesets, issues and PRs do not exist here.

## 2. Isolation rules for this edition

1. Remotes: `origin` = `kintsugi-omega-master` only. Check with `git remote -v` before every push.
2. neatlogs: create a **separate neatlogs project** for this edition and put its `NEATLOGS_PROJECT_ID` and tokens in this clone's own `.env`. Never reuse the #neatHack project's tokens; the clone starts without a `.env`, so nothing reports anywhere until you add one. Re-provision receptors with `kintsugi senses provision` against the new project.
3. GitHub: rulesets, issues and PRs are created on this repository only (`ShipConfig`/`--github suryaprasanthcse/kintsugi-omega-master`).
4. Keys: keep using `%USERPROFILE%\.kintsugi\keys\`. The holdout is still sealed to the rehearsal identity (`holdout-rehearsal.agekey`), which is this repository's `HOLDOUT_AGE_IDENTITY` secret.
5. Sole contributor: every commit authored `Surya Prasanth R <suryaprasanthcse@gmail.com>`; never a `Co-authored-by:` trailer (`.claude/settings.json` has `includeCoAuthoredBy: false`).

## 3. Working protocol (unchanged from Stages 1–8)

- Graph first: `entire graph search --repo . --profile full --query "…"` (see `.entire/graph-agent.md`).
- Every tracked change through Edit/Write; shell rewrites only on files Edit/Write also touched in the same commit (A-16, A-37).
- Commit with `ENTIRE_TEST_TTY=0`; after each commit check one `Entire-Checkpoint` trailer, the ref exists, and `files_touched` covers the commit:
  ```bash
  ID=$(git log -1 --format='%(trailers:key=Entire-Checkpoint,valueonly)' | tr -d '\r\n '); git show-ref | grep -c "$ID"; uv run python -I -c "from kintsugi.memory import entire; import subprocess; t=set(entire.read_checkpoint('.', '$ID').metadata.get('files_touched') or []); d=set(subprocess.run(['git','show','--name-only','--format=','HEAD'],capture_output=True,text=True).stdout.split()); print('missing:', sorted(d-t))"
  ```
- Targeted tests per commit; the full suite (about 14 min) once per stage on the pushed HEAD; omega-ci after every push.
- Secret-scan pushed content (`kintsugi.proof.redaction.scan_text` on the added lines, `assert_pushable` on stale checkpoint refs), then `git push origin main` and `entire.sync_checkpoint_refs('.')`.
- Subprocesses only through `kintsugi/sandbox/proc.py`; never print secrets; never work around a usage-policy refusal; workflow changes behind `workflow.patched`.
- **A-57:** the real sabotage mechanisms (S-17/18/19) are never written in clear into this repository or the fleet. This file deliberately does not describe them.

## 4. Execution plan

Recommended order: **13a** (runtime wiring) → **9** → **12** → **10** → **11** → **13b** (dress rehearsal, T0) → W. Stage 9 and Stage 12 are internal and pairable (strategy rule: start after a quota reset, < 30% context, separate commits, clean tree between them); 10 and 11 are outward-facing and must never be paired.

### Stage 13a — Runtime wiring for W

1. **The W crew factory** (`kintsugi/crew/runtime.py`, a `module:factory` for `kintsugi control watchdog --crew` and the worker):
   - `BranchLedger(repo, Window.genesis())` replaces `FileLedger`; every `ledger_append` becomes one commit on `refs/heads/ledger`.
   - `Anchoring(Ed25519Signer.load(<T0 key outside the repo>), repo_url, out_dir)`; start the `AuditAnchor` workflow at T0 so `do_anchor_ledger` signs `anchor-YYYYMMDD-HH` hourly; `BranchLedger.push()` after each anchor (fast-forward only); write the post text for the Architect.
   - `SabotageLane(Saboteur(store, (t0_recipient,), fleet), identity, deliver, publish)`.
2. **The `deliver` port**: `git worktree add` off `origin/main` on an `upstream/<slug>` branch → `Notary.commit(worktree, Saboteur.delivery_request(spec, patch), package_root=f"hosts/{host}")` → push → `GhCli.pr_create` → merge when the host's normal CI is green (the upstream lane: Omega's gates do not apply, §6.2 step 3). The ledger records `sabotage/delivered`; C6 checks the order.
3. **`publish` port** (only for a seal inside W): a notary session applying `Saboteur.commitment_request(record)` in a worktree, scopes `surgeon` + `eval_augment`.
4. **S-17/18/19**: compose the three specs with `kintsugi.crew.saboteur.compose` from the blueprint's §6.2–§6.4 scenarios, privately; write each patch outside the repo; `Saboteur.seal` into `%USERPROFILE%\.kintsugi\sabotage\` with the T0-held age recipient; commit only `sabotage-log/<id>.json`. Verify each with `realism_problems` and the rubric. Optional: wire the Haiku realism rubric as a `Rubric` (≥ 0.7) with the deterministic fallback kept.
5. **Chaos-proxy deliveries** (`env.*` classes) for the Saboteur, if a sabotage needs them.
6. **Runtime in WSL** (D2): install the Temporal CLI in WSL Ubuntu, then a `scripts/` launcher for Temporal, the worker, Watchdog, ingress, poller and `fleet up` with keep-awake.
7. **Verified by:** a disposable end-to-end rehearsal (practice host): genesis → sealed practice spec → delivery PR → incident → resolution → anchors → `kintsugi audit --all` exit 0 on that rehearsal's window record.

### Stage 9 — Economic cortex and cfo.ai (L5)

- **Builds:** `kintsugi/econ/{governor (live), actuals, wright (nightly refit), cfo_sync}.py`, `business/{plan.json, actuals.json, actuals.csv, cfo_export/}`, the L5 treatment path with its decision span.
- **Verified by:** plan hash pinned per incident; C10 within 1% against the new neatlogs project's session costs; the governor never lowers power below 0.8 and escalates instead; L5 rehearsal on Triage; cfo.ai Unit economics / Runway / Scenarios tabs with plan-vs-actual. The rehearsal costs above ($0.665 vs $0.089/$0.104) seed the unit-economics model.

### Stage 12 — Self-repair (INC-SELF-01)

- **Builds:** `hosts/omega`, a replay suite of 20 incidents recorded in the Stage 6–9 rehearsals, a grader (root-cause hit@3, cost, p95), enforcement of the `self_repair` protected scope (`.kintsugi/protected_paths.yaml`).
- **Verified by:** a self-repair rehearsal with before/after trace sets; audit C14 blocks any diff touching `kintsugi/proof`, `kintsugi/stats`, `kintsugi/gates`, `schemas`, the gateway or `.kintsugi`.

### Stage 10 — Onboarding O1–O7, Repair Clinic, open-source hosts

- **Builds:** `kintsugi/onboard/{intake,instrument,eval_synth,baseline,immune_scan,replay_mode}.py`, the Onboarding workflow, a Clinic intake issue form with consent checkboxes, PR-only receipts for foreign repositories (`ShipConfig.pr_only`).
- **Verified by:** OSS-1 onboarded end to end in the sandbox; replay mode from exported traces; the code refuses to merge into a foreign repository. Outward-facing: every foreign post needs the Architect's approval.

### Stage 11 — Arena and public API

- **Builds:** `arena/` (fleet board, incident stream, immunity matrix, learning curve, ledger head, "Sabotage it"), `kintsugi/arena_api.py` (a strict-schema compiler to `saboteur.CATALOG` parameters only, sealing on arrival through `SabotageCampaign.request`), rate limiting, a daily cap.
- **Verified by:** an injection fuzz corpus compiles only to catalog values or is rejected; 1 request per IP per 10 minutes; escalations displayed.

### Stage 13b — Dress rehearsal, T0, then W

1. A compressed L0–L5 + self-repair dress rehearsal; `kintsugi audit --all` green on it.
2. `kintsugi t0 check` until only confirmations remain, in this order: T0 attestation key (`kintsugi keys generate`, private key outside the repo), commit `.kintsugi/attest.pub` and post its fingerprint; re-seal the holdout to a new Architect-held identity, push, then rotate `HOLDOUT_AGE_IDENTITY`; create the ruleset on `main` after the last direct push; seal S-17/18/19; `kintsugi t0 genesis --ruleset <id>`, commit the window record (the T0 commit), push `ledger`; runtime, background chaos, tunnel (`cloudflared`) and `kintsugi senses provision --public-url`.
3. W: no build session writes to the repository; the Architect posts the hourly anchor text; after W, set `t1`, publish the reveals and bundles, run `kintsugi audit --all` and `make judge`.

### State after the 2026-10-13 overnight run

- **Built:** `kintsugi/crew/{runtime,upstream,w_rehearsal}.py`, `scripts/w-runtime.sh`,
  `scripts/keep-awake.ps1` (13a); `kintsugi/econ/{actuals,cortex,cfo_sync}.py`, the Wright refit,
  `kintsugi/crew/{treatment,l5_rehearsal}.py`, `business/` and `kintsugi econ` (9);
  `self_repair` enforcement, `hosts/omega`, `kintsugi/crew/self_repair_rehearsal.py` (12);
  `kintsugi/onboard/`, the Clinic issue form, `kintsugi/arena_api.py` (10, 11 offline).
- **Evidence:** `evidence/rehearsals/{w-20261012-193427,l5-20261012-200248,self-20261012-202518}.json`.
- **Tests:** 901 passed, 30 skipped on `37e0be7`, plus 43 Wave D tests; omega-ci green from
  `37e0be7` on (red on `01f9f5a` and `37304be`, fixed there).
- **Waiting on the Architect:** S-17/18/19 (compose and seal outside any Entire-tracked session;
  A-69); the Temporal CLI in WSL; this edition's neatlogs project and `.env` (C10, receptors,
  the ingress); cfo.ai/Ari access; the cash and fixed costs in `business/plan.json`; the OSS-1
  choice; the Arena board; then the 13b dress rehearsal, which also records more replay
  incidents (4 of 20 today).

## 5. Open issues carried over

- A-38 neatlogs alert rules silent; A-39 3–4% trace loss; the ingress webhook points at a dead quick-tunnel URL (re-provision in the new neatlogs project).
- C7–C10 and C12 live ports (`gh api`, neatlogs) have only run against recorded answers.
- P1: per-incident proof bundles (`evidence/<INC>/bundle.json`) and `make judge` over `published-bundles/`.
- An orphan checkpoint `01M4QS4028Z1NPCGW09SMPM8YC` (an undone, never-pushed commit) exists among the checkpoint refs, linked to no commit.
