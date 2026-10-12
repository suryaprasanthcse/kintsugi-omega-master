# Stage verification records

Each stage's verification targets (from `docs/EXECUTION_PLAN.md` §4) with the result and the
evidence that reproduces it. Numbers are copied from committed artifacts or from the named test.

## Summary for #neatHack judges

| Claim | Where it is verified |
|---|---|
| A crew repaired a real agent regression and proved it with gates G0–G7 | Stage 6 (`INC-R235459-0001`, $0.665) |
| The fix became an antibody that matured on another host (PR #7, G4 p=0.0156), scored receptor precision 1.00 over 335 runs and found 6 silent past failures ($12.00) | Stage 7 |
| Repeat repairs cost ~7× less: vaccinations $0.089 and $0.104 vs $0.665 | Stage 7, `evidence/rehearsals/l3-20261012-074523.json` (`costs`) |
| A harmful vaccination was caught by the canary and auto-reverted through an attested notary session (PRs #8–#10, a **disclosed drill**) | Stage 7 |
| `kintsugi audit` C1–C14 catches tampering of every kind it checks | Stage 8, 33 mutation tests |
| The autonomous window W was **not run**; Stages 9–12 are **descoped** | Architect's go/no-go, 2026-10-12; README "Disclosure" |
| Master Edition (after the submission): Stages 13a, 9 and 12 and offline Stage 10/11 modules built and verified offline | "Master Edition overnight run" below |

## Master Edition overnight run (2026-10-13): Stages 13a, 9, 12, offline 10/11

Recorded 2026-10-13 00:20–02:40 IST on Windows 11 (Entire 0.11.4, entire-graph v0.4.0) in an
unattended session the Architect authorised. Amendments A-69 … A-72. No headless model session
ran (the headless CLI is not logged in on this machine, and every rehearsal is offline by
design); no `.env` exists in this clone, so nothing reached neatlogs, cfo.ai or a foreign
repository. Every commit carries one verified `Entire-Checkpoint` whose `files_touched` covers
it.

| Target | Result | Evidence |
|---|---|---|
| 13a W crew factory | ✅ `kintsugi.crew.runtime:crew` builds the crew on the `ledger` branch from the window's genesis; refuses a missing or closed window, a ledger without genesis, keys inside a git worktree and a T0 key that is not `attest_pubkey`; anchors push ledger and tags fast-forward; the Watchdog passes `KINTSUGI_RUNTIME` to its pools | `tests/crew/test_runtime.py`, `tests/control/test_service.py` |
| 13a deliver / publish ports | ✅ notary commit in a worktree off the fetched base → `upstream/<host>-<minute>` → PR → merge commit only on green CI; red or silent CI never merges | `tests/crew/test_upstream.py` |
| 13a WSL launcher | ✅ valid bash; every `kintsugi …` command it runs parses; installs nothing | `tests/test_scripts.py` |
| 13a end-to-end rehearsal | ✅ (offline) genesis → sealed practice spec → upstream-lane delivery → anchor → t1 + reveal: audit **C1 PASS** (3 entries, 1 anchor), **C6 PASS** (1/1, 0 honeytoken hits); C2–C5, C7–C10, C12, C13 need real sessions, GitHub, neatlogs or a holdout → 13b | `evidence/rehearsals/w-20261012-193427.json`, `tests/crew/test_w_rehearsal.py` |
| 9 plan hash pinned per incident | ✅ `business_plan_sha` in every incident plan | `tests/econ/test_cortex.py` |
| 9 actuals | ✅ 4 recorded rehearsal spends with sources; case-file cost preferred ($0.665376); rehearsals never enter `spent_mtd` | `business/actuals.json`, `tests/econ/test_actuals.py` |
| 9 governor never lowers power below 0.8 | ✅ n stays Connor's full-power n (221 pairs at r_fail 0.05); "repair with lower n" is always rejected; the incident escalates | `evidence/rehearsals/l5-20261012-200248.json` |
| 9 L5 rehearsal on Triage | ✅ (offline) measured 9/180 (5.0%) → B $2.25, T1 $3.18 → **escalate**; spec §6.8 inputs (disclosed) → B $4.83, T1 $2.34 → **treat and escalate**; T1 applied through the Treater; the same live traffic then fails **5/180** | same file, `tests/crew/test_l5.py` |
| 9 cfo.ai tabs | ✅ as files (Ari MCP, A8, not wired): unit economics, scenarios, runway (waits on the Architect's cash), plan vs actual, Wright refit (b = 2.30 over 4 points, wide intervals) | `business/cfo_export/`, `tests/econ/test_cfo_sync.py` |
| 9 C10 within 1% against the new neatlogs project | ⏳ needs this edition's neatlogs project and `.env` | — |
| 12 self_repair scope | ✅ enforced at the Surgeon's commit, the diff check, the cards and treatments; refused for each of `kintsugi/proof`, `stats`, `gates`, `sandbox/proc.py`, `schemas`, `.kintsugi`, workflows, the plan; C14 fails any such self-repair | `tests/gates/test_self_repair.py` |
| 12 replay suite and grader | ✅ `hosts/omega`: **4 recorded incidents of 20** (none invented), hit@3 / cost / p95 | `tests/omega/test_replay_suite.py` |
| 12 self-repair rehearsal | ✅ (offline, prepared patch disclosed) hit@3 **0.25 → 0.75**, C14 PASS 1/1, an audit edit refused at commit; main unchanged | `evidence/rehearsals/self-20261012-202518.json`, `tests/crew/test_self_repair_rehearsal.py` |
| 10 onboarding (offline) | ✅ consent-gated intake, health card, replay mode = live path, seed evals, baseline, immune scan, PR-only terms | `tests/onboard/test_onboard.py` |
| 11 Arena API (offline) | ✅ 33-payload injection fuzz corpus refused; environment classes only; sealed on arrival; 1/client/10 min; daily cap | `tests/test_arena_api.py` |
| Full suite | ✅ **901 passed, 30 skipped** on `37e0be7` (before the Wave D tests, which add 43) | `uv run pytest -q`, 2026-10-13 |

### Defects found while building, all fixed

- The Watchdog's subprocess gateway would have dropped `KINTSUGI_RUNTIME` from its pools.
- The test helper's fake notary missed files inside new directories (`git status -uall`).
- The offline notary stand-in stripped the first `git status` line's leading space and lost a
  path; caught by the rehearsal's own commit refusal (A-16).
- Adding `hosts/omega` broke `t0 check`'s baseline count: omega-ci red on `01f9f5a` and
  `37304be`, fixed in `37e0be7`.

### Known limits carried forward

- S-17/18/19 are not composed or sealed (A-69 (5)); the Temporal CLI in WSL is not installed.
- The replay suite has 4 of 20 incidents; Triage's root cause is still missed after the
  self-repair (hit@3 0.75).
- cfo.ai Ari MCP, live C10, OSS-1, the Arena board and its model compiler, chaos-proxy
  deliveries: not built (A-70, A-72).

## Stage 8 — Proof plane: audit C1–C14, ledger branch and anchors, Saboteur, T0 preflight

Recorded 2026-10-12 (IST) on Windows 11 with Entire 0.11.4, entire-graph v0.4.0, Claude Code CLI
2.1.296 and temporalio 1.34.0. Deviations: amendments A-67 (ledger, anchors, window record,
audit semantics) and A-68 (the Saboteur). Stages 9–12 are descoped by Architect decision
(strategy §5), disclosed. No headless model session ran in this stage: every check below is
offline or uses recorded API answers, except `t0 check`'s live read of the rulesets API.

### Targets

| Target (plan Stage 8) | Result | Evidence |
|---|---|---|
| Every check C1–C14 passes clean and fails tampered | ✅ reordered ledger, re-chained rewrite caught by its anchor, forged anchor, missing anchor, unrecorded bundle (C1); human commit, doctored merge (C2); forged signature (C3); missing nonce (C4); prompt mismatch (C5); changed reveal, wrong class family, delivery before seal, unrevealed after W, **honeytoken positive control** in a checkpoint transcript (C6); missing/failed G7 run (C7); unknown trace, trace outside the incident window (C8); unclaimed hit, incident missing from the ledger (C9); cost Δ 2.0% (C10); five lineage gaps on a copy of the real AB-1 plus a missing version file (C11); ruleset disabled inside W, created after T0, covering another branch, none recorded (C12); swapped sealed file, manifest changed since T0 (C13); self-repair touching `kintsugi/proof/` (C14) | `tests/proof/test_audit.py` (33) |
| Offline mode needs no credentials; exit codes 0/1/2 | ✅ `--offline` builds no API port; a check without its inputs is SKIP, never PASS; 1 if any FAIL, else 2 if any SKIP | `test_exit_codes`, `tests/test_cli.py::test_audit_before_t0_skips_what_needs_the_window` |
| `kintsugi audit --all --offline ./published-bundles` today | exit **2**: C1–C10 and C12 SKIP (no window record before T0); C11 **PASS** (1 antibody, 1 quarantine: the real AB-1 lineage replays); C13 SKIP (3/3 manifests match, no T0 digest yet); C14 PASS (0/0) | run 2026-10-12 15:00 IST |
| Ledger on the append-only `ledger` branch (D5) | ✅ plumbing commits with compare-and-swap `update-ref`; `main` and the working tree untouched; one signed `anchor-YYYYMMDD-HH` per hour, idempotent on retry; C1 reads branch and tags | `tests/proof/test_ledger_branch.py` |
| Genesis and window record | ✅ `kintsugi t0 genesis` writes `.kintsugi/window.json` and seq 1 linked to h_0 from the committed T0 key; refuses twice; the T0 commit is W's base | `tests/control/test_genesis.py` |
| Saboteur: catalog, realism, rubric ≥ 0.7, sealing, A-52 delivery | ✅ 17 Appendix B classes; rules reject non-catalog, outside-fleet, protected-path, out-of-host, destructive and leaky specs; deterministic rubric (no model); age store refuses a git worktree; the store holds neither honeytoken nor mechanism in clear; delivery prompt = framing + patch, leak-checked | `tests/crew/test_saboteur.py` (14) |
| Sealing dry run on a practice spec | ✅ disclosed practice spec `P-1` (`code.regression.exception_swallow` on a fictional host, never S-17) sealed into `~/.kintsugi/stage8/practice/store` (`P-1.age`, `P-1.patch.age`, public `P-1.json`), reopened with the practice identity, revealed; the delivery purpose is "Simplify error handling in fetch_all. Apply this patch and run the existing tests."; audit C6 **PASS** (1/1 commitments, 0 honeytoken hits) | run 2026-10-12 15:36 IST, outside the repository |
| `kintsugi t0 check` lists what remains | ✅ read-only; 2/11 done today (baselines, holdout manifest); the remaining items are Architect actions (holdout re-seal, T0 key, genesis, ledger push, sealing S-17/18/19, ruleset on `main`, chaos, runtime, tunnel/webhook) | `tests/control/test_preflight.py`; run 2026-10-12 |
| Full suite on the pushed HEAD | ✅ **842 passed, 30 skipped** (the contract tests) in 13 min 50 s on `7a2fc99`, run once | `uv run pytest -q`, 2026-10-12 15:36–15:50 IST |

### Defects found while building, all fixed before commit

- C13 raised on a repository without a holdout manifest instead of failing (found by the
  window test).
- The genesis design first counted the T0 commit itself inside W (it would have shown as a human
  commit); W's base is now the commit that first added the window record.
- Reveal files in `sabotage-log/` would have been read as commitments.
- A Saboteur fixture sealed after its own `not_before`; C6 caught it.
- One commit (`bf5e614`, never pushed) had two files changed only by shell rewrites (A-16/A-37);
  it was undone and re-applied with Edit so `files_touched` is complete (`7a2fc99`, after one
  amend that dropped the undone commit's copied trailer). The undone commit's checkpoint
  `01M4QS4028Z1NPCGW09SMPM8YC` stays on the checkpoint branch, linked to no commit on `main`.

### Known limits carried forward

- Online checks C7, C8, C9, C10 and C12 are exercised with recorded API answers; their live
  ports (`gh api`, neatlogs) first run against W. C9's online port enumerates detections from
  the senses store's claimed hits.
- The Haiku realism rubric is not wired (the deterministic fallback is the rubric, A-68).
- `deliver(request, host)` and `publish` are runtime ports; the W crew factory binding a
  `BranchLedger`, `Anchoring` and `SabotageLane` (worktree, notary, upstream-lane PR) is Stage 13
  work, as are chaos-proxy deliveries.
- P1 (per-incident proof bundles, `make judge` over `published-bundles/`) is not built; `make
  judge` runs the audit, which SKIPs what bundles would supply.

## Stage 7 — Canary, antibodies, motif engine, vaccination, quarantine (L3)

Recorded 2026-10-12 (IST) on Windows 11 with Entire 0.11.4, entire-graph v0.4.0, the headless
Claude Code CLI **2.1.296** (the Opus roles on `claude-opus-5-5` natively, A-59), temporalio
1.34.0 (Temporal CLI 1.9.1 dev server in the rehearsals) and GitHub. Deviations: amendments A-59
to A-66.

### The L3 rehearsal (`kintsugi crew l3`, disposable hosts, disclosed drill, A-61, A-66)

AB-1 is synthesised from the Stage 6 incident that resolved (`INC-R235459-0001`, winner
`e50387e`), matured on the CRM practice host (the Clinic stand-in), and sent through a
`VaccinationWave` under a `FleetSupervisor`. On Scout the drill injects the recipe's literal
application past the gates; everything after that merge is Omega's own response. Every run:

| Run | What happened | Outcome |
|---|---|---|
| `l3-20261012-073800` | AB-1@1.0 synthesised on the real graphs. Maturation failed on both hosts: the CRM vaccination's G0 cassette mixed duplicate failures with retry-exhaustion runs (cassette misses after any fix: G2 0/3), and the standalone Scout copy ran ruff with default rules (G1). The natural recipe session on Scout also failed G2 (its 28-line hoist-and-resume made requests the trigger never recorded). | CANDIDATE; fixed in `010bdb6` |
| `l3-20261012-074523` | **Complete** (below). One defect: the wave re-vaccinated the CRM host it had just matured on (a fleet scan cached across workflows; harmless here, PR #7 reused). | AB-1@1.1 ACTIVE; fixed in `9caab82` |
| `l3-20261012-080554` | Matured and backfilled as run 2, then the API refused the drill's delivery session (a usage-policy classifier flag on Scout's corpus text, after the edit was applied); the activity retry met the stale worktree. Not worked around. | aborted; retry fixed in `467cbad`; PR #11 closed |

`l3-20261012-074523` (`evidence/rehearsals/l3-20261012-074523.json`, `antibodies/AB-1`), 962 s:

1. **Synthesis** (§6.5 step 12): motif `state-reset-on-retry` binds `R=retry_with_backoff,
   Enc=reconcile, F=book_pending_lines, S=PostingSession` on the origin's graph before the fix
   and not after it. Recipe drafted by `claude-opus-5-5` with `{S} {F} {R} {Enc} {intent}` slots.
   Receptor `^"?FAIL:DUPLICATE_` on `grader.verdict` (precision preview 0.967: the CRM host's
   duplicates were still unexplained).
2. **Maturation** (step 13) on `crm-practice`: the induced trigger (30% 429s on `geo`) failed
   12 runs (7 `DUPLICATE_CONTACT`, 5 `RATE_LIMITED`), the G0 cassette failed 3/3; a Sonnet
   Surgeon applied the recipe in 3 lines (checkpoint `01M4QH8JS6VN66N9CJ4GPTZ413`); the blind
   Auditor passed **G0–G6** (G4: b=6, c=0, p=0.0156, pass 0.95 → 1.00, cost UB −0.8%, p95 UB
   −0.2%); [PR #7](https://github.com/suryaprasanthcse/kintsugi-omega/pull/7) opened, never
   merged. CANDIDATE → MATURED → ACTIVE on receptor precision **1.00 over 335 recorded runs**.
3. **Backfill**: **6 silent past occurrences** on the CRM host's baseline runs, $12.00 impact
   (Omega-side retro scan, A-64).
4. **Wave** (step 14): the scan matched Scout; Scout fails the induced trigger too (6
   `RATE_LIMITED`: each retry restarts at page one), so the rule admits it (A-63).
5. **Drill**: a notary session delivered the literal recipe (`ScratchBuffer` hoisted, the loop
   unchanged). The Auditor **refused it** (G2 0/3); the drill merged it anyway as
   [PR #8](https://github.com/suryaprasanthcse/kintsugi-omega/pull/8).
6. **L3, unscripted** (§6.7): Scout's canary against Scout's own H0 (p0 0.031) **rejected at
   n=38, LLR 3.118**; the Reverter's notary committed exactly `git revert -m 1` (checkpoint
   `01M4QHYXS4HVEK486YRWE86TH8`), expedited G1 and G2 (6 canary failures replayed, all pass),
   [PR #9](https://github.com/suryaprasanthcse/kintsugi-omega/pull/9) merged; the **reverse
   canary accepted at n=70** (LLR −2.276); `pause_antibody` reached the wave through the
   supervisor; AB-1 → **QUARANTINED** with its receptor kept; PR #7 received the "On hold"
   notice; trace-to-test committed 6 seed-addressed cases
   ([PR #10](https://github.com/suryaprasanthcse/kintsugi-omega/pull/10), checkpoint
   `01M4QJ1G61A0DQ5S3HT8WTW8MX`); contrastive learning over `ledger-practice@pre-fix`,
   `ledger@pre-fix` and `crm-practice` against Scout selected **"a side-effecting callee inside
   the retried scope"** (the other three candidates each fail on `ledger@pre-fix`), Opus wrote
   the rationale, the Auditor's re-match held; **AB-1@1.1** re-matured on the CRM host →
   ACTIVE, PR #7 told it resumed, and the wave's 1.1 re-scan **excluded Scout**.

### Targets

| Target | Result | Evidence |
|---|---|---|
| Motif validation: matches before the fix, not after, precision ≥ 0.9 | Before ✓, after ✗ on the origin's real graphs; the 1.0 scan's matches (CRM, Scout) both fail the induced trigger, so the admission precision is 2/2; 1.1 keeps 3/3 positives and excludes Scout | `evidence.json` `motif_validation`; `tests/memory/test_motif_positives.py`, `test_contrastive.py`, `test_antibody_library_ab1.py` |
| Induced-trigger rule | CRM: 12 induced failures, G0 3/3; Scout: 6 (`RATE_LIMITED`); a host without a failure is never vaccinated | lineage `induced-trigger` events; `tests/crew/test_immunologist.py` |
| Receptor precision ≥ 0.9 | 1.00 on 335 recorded runs (Omega-side, A-29/A-64) | lineage `receptor-precision`; `test_scan_backfill_and_promotion` |
| A backfill completes | 6 silent occurrences, $12.00 | `evidence.json` `backfill` |
| L3: canary reject → revert PR → reverse canary → `pause_antibody` → PR notices → QUARANTINED, receptor active | All, in that order (n=38 reject; PR #9; n=70 accept; PR #7 comments) | `evidence.json` `autoimmunity`; `test_an_autoimmune_vaccination_is_reverted_quarantined_refined_and_the_wave_resumes`, `test_a_paused_antibody_reaches_its_running_wave_through_the_supervisor` |
| Contrastive learner separates 3 positives from Scout; 1.1 excludes Scout | Yes; selected `side_effect_in_scope` | `refinements[0]` in `antibodies/AB-1/lineage.json` |
| Consistent lineage | Versions 1.0 → 1.1, one quarantine, every state move CANDIDATE → MATURED → ACTIVE → QUARANTINED → ACTIVE logged with its reason; 1.0 stays resolvable | `test_ab1_is_active_at_1_1_with_its_history` |
| Secondary responses far cheaper than primary | Primary (diagnosis, three Surgeons, gates) $0.665; vaccinations $0.089 (CRM) and $0.104 (Scout drill): about 7× cheaper | `evidence.json` `costs` |
| Canary H0 per host | Wilson 95% upper bound of the recorded train failure rate: ledger 0.0158, scout 0.031, triage 0.0972 | `tests/gates/test_canary.py` |

### Invariants and proof

- **I1**: the injected drill merge is the only change that bypassed G0–G6, on an unprotected
  rehearsal branch, disclosed (A-66) and recorded with the Auditor's refusal; every other
  change (the CRM vaccination, the revert, the eval-augment) passed its gates.
- **C4**: every rehearsal commit is attested and checkpointed: Surgeon, notary revert, notary
  eval-augment and the drill delivery; a revert whose tree differs from git's is refused
  (`test_a_notary_that_changes_anything_else_is_refused`).
- **I5**: trace-to-test uses train tasks only; the `eval_augment` scope forbids code, tests,
  graders and the holdout (`test_eval_augment_adds_regression_data_only`).
- **§5.7 matrix**: the Immunologist cannot read Surgeon reasoning
  (`test_the_immunologist_never_reads_surgeon_reasoning`).
- Suite on `467cbad`: `ruff`, `ruff format --check`, `pyright` and the prompt pins clean;
  `pytest -q` **786 passed, 30 skipped** in 14:05 (the 30 are contract tests; this stage's
  graph goldens and AB-1's behaviour were verified live). The secondary-response test added
  afterwards caught a real defect (the Immunologist could not write the recipe prior) and
  passes with its fix.

### Known limits carried forward

- L3 is a disclosed drill: in this fleet the gates refuse the wrong vaccination (A-66).
- The CRM practice host stands in for Clinic-A until Stage 10 onboards real Clinic/OSS hosts.
- The incident-path secondary response (`match_antibody` → the recipe as the Coordinator's
  prior) is covered by a unit test, not yet by a live rehearsal.
- AB-1's receptor is drafted (`receptor.json`), not created in the neatlogs project; its
  precision and backfill are Omega-side (A-29, A-64). The semantic retrieval is deterministic
  over signature fields, not neatlogs' semantic search.
- Scout's own weakness (retries that restart at page one exhaust the budget) is a separate
  pathogen class, not yet opened as an incident.
- Rehearsal drill branches run no G7 and no ruleset (the G7 path was proven in Stage 6).
- Issue #6 and PR #7 stay open: a foreign host's vaccination is a pull request only.

## Stage 6 — Crew, primary incident loop, gates G0–G7, ship (L0, L1, L4)

Recorded 2026-10-12 (IST) on Windows 11 with Entire 0.11.4, entire-graph v0.4.0, the headless
Claude Code CLI 2.1.269, temporalio 1.34.0 (time-skipping server in tests, Temporal CLI 1.9.1 dev
server in the rehearsals) and GitHub Actions. Spikes: `docs/SPIKES.md` (Stage 6); deviations:
amendments A-47 to A-58. Crew models: Sentinel `claude-haiku-5-5`, Surgeons
`claude-sonnet-5-5`, the Opus roles on `claude-opus-5` (A-54).

### The Ledger rehearsal (disposable, disclosed practice target, A-57)

`kintsugi crew rehearse` copies the Ledger practice host outside every repository, delivers the
practice regression through a headless session (attested, with its own checkpoint), runs live
traffic and sends the failures as R-007 hits through the FleetSupervisor on the dev server. Every
attempt is recorded, including the ones that found bugs in Omega:

| Run | What happened | Outcome |
|---|---|---|
| `reh-20261011-224740` | The delivery model refused the patch: its framing promised "keep behavior identical" (A-52). | no incident |
| `…-224903`, `…-225031` | The ruleset's glob blocked the push creating the run's base (N9: a required check also blocks creation). | no incident |
| `…-225239` | Opus calls refused by the CLI (A-54), deterministic fallbacks answered; round 0: three passes, a pre-registered dispute picked H1; G7 in Actions failed its fresh-seed G2 (2/3: the stress profile exhausted a correct retry budget). PR #2 closed with the reason; G2's criterion fixed (A-53). | ESCALATED, nothing merged |
| `…-232114` | Real crew. **Round 0:** H1 (hoist), H2 (revert), H3 (idempotent pass) all passed G0–G4 and G6, but G5 raised intent concerns on all three → no winner, **rewound** (attempt refs kept). **Round 1:** H4 (refined hoist) kept every clause; H5 drew a concern; H6 failed G1. H4 won. PR #3: G7 **passed** in Actions, but the shipper read a skipped job of the same name as not passed. | ESCALATED (I1 held: no merge without a readable G7); fixed in `7d1ec66` |
| `…-235459` | **Resolved end to end** (below). | RESOLVED |

`reh-20261011-235459` (`evidence/rehearsals/reh-20261011-235459.json`), 554 s, $0.65 metered:

| Step | Result |
|---|---|
| Detection | 35 failed live tasks → 35 R-007 hits; the Sentinel paged at 3 in 10 minutes: 1 opened, 34 attached (I7: every hit disposed) |
| Pathologist | one cluster, φ `sha256:b9fe39c5…`, "Duplicate books:/lines write after fx_rate lookup"; **G0: the cassette fails 3/3** at seed 1 on the base |
| Archivist | intent record from checkpoint `01M4PP8XBXDA2H5E0472GM1RYP` (the delivery): 5 contract clauses, **every quote verified verbatim** against the transcript (SHA-256 recorded); the commit trailer and `entire why` **agree**; regression confirmed, **first bad checkpoint** = the delivery's |
| Plan | B = $17.93 (Cap; VaR higher), K = 3, n = 14 pairs needed (120 available), plan `sha256:b34e37c9…` |
| Surgeons | three headless Claude Code sessions in parallel worktrees, **each an attested commit with its own checkpoint** (`01M4PPD46B…`, `01M4PPD6W7…`, `01M4PPDS7S…`); **C2–C5 pass on all three** and on the delivery |
| Auditor | all three pass G0–G4 and G6 (G4: b = 39, c = 0, p = 1.8×10⁻¹²; cost Δ −17%; p95 Δ UB −32%); G5: H1 concern (Opus), H2 and H3 kept every clause (deterministic fallback: the two Opus calls hit the subscription's session limit, recorded in `calls.jsonl`) |
| Dispute | H2 and H3 on the Pareto front → protocol registered (`sha256:66fa65ca…`) before running: stress profile, the same tasks and seeds: 11/12 vs 11/12, cost equal → **smaller diff: H2** (10 vs 18 lines). H2 keeps the refactor's name and docstring and moves the session back outside the retried pass. |
| Ship | issue #4 → **PR #5** whose body is the E.4 receipt (`Fixes #4`) → **G7 green** in Actions (run 38186481441: G1 ✓; G2 no `DuplicateEntry` on the head at 3 fresh seeds, 4 on the base; G3 0 flips; G4 b = 28, c = 0, p = 3.7×10⁻⁹) → merged with merge commit `76f19d1` (second parent: the attested commit) |
| Canary | live batches on the merged code; SPRT accepted H0 → RESOLVED; antibody placeholder recorded for Stage 7 |
| I5 | holdout canary hits: case file 0, crew calls 0, Surgeon transcripts 0 (3 scanned) |
| Ledger | 8 hash-chained entries (transitions, incident, antibody), verifies |

### Targets

| Target | Result | Evidence |
|---|---|---|
| G0 fails 3/3 | ✅ live and in tests (a healthy base does not reproduce) | `test_g0_reproduces_the_regression_and_g2_accepts_only_the_fix`; rehearsal |
| Intent Record with verified quotes | ✅ live (5 clauses, trailer and `entire why` agree); invented quotes are dropped | `test_the_archivist_writes_a_verified_intent_record` (contract), `test_editing_turns_quotes_and_heuristics` |
| Three parallel Surgeons, each with a checkpoint | ✅ live, C2–C5 pass on every attempt | rehearsal evidence `hypotheses[].c2_c5` |
| Round 1 fails and is rewound → round 2 | ✅ live (`…-232114`: three G5 concerns, rewound, round 1 winner) and in the deterministic loop test (naive hoist flips G3, lookup breaks tests, revert ranked down) | `evidence/rehearsals/reh-20261011-232114.json`; `test_round_one_fails_round_two_disputes_then_ships` |
| Pre-registered dispute experiment | ✅ live (twice) and in tests; a protocol changed after registration is refused | `test_the_decision_rule_and_a_pre_registered_dispute` |
| Ship → G7 green → merge | ✅ PR #5, run 38186481441, merge `76f19d1`; locally `LocalShipper` runs G7 in process | `tests/control/test_ship.py` |
| Protected-path edits blocked twice | ✅ the pre-commit hook refuses them on `kintsugi/*` branches; the Auditor's diff check fails a commit that bypassed it; `orchestrator_commit` refuses them before committing | `test_the_precommit_hook_refuses_protected_paths_on_surgeon_branches`, `test_the_auditor_is_blind_and_strict`, `test_the_surgeon_runner_commits_an_attested_change` |
| Diffs over 120 lines blocked | ✅ unless the card records the Coordinator's approval | `test_antigaming_blocks_protected_paths_big_diffs_and_network`, `test_the_plan_and_the_cards` |
| New outbound network calls blocked | ✅ HTTP clients, sockets and URL literals in added lines | same antigaming test |
| Zero holdout leaks across crew spans and transcripts | ✅ live scan above; the loop test scans the case file and every crew prompt; G4 refuses an output carrying the canary or a task id | `test_round_one_fails_round_two_disputes_then_ships`, `test_g4_accepts_the_hoist_and_rejects_the_costly_lookup` |
| The Auditor never sees Surgeon output | ✅ enforced by the case file (refused reads are logged); live: the Auditor's reads are card, repro, cassette, intent record, cause report only | `test_the_auditor_cannot_read_surgeon_output`, rehearsal `information_flow` |
| G6 stops a round when the budget is spent | ✅ no Surgeon session starts past B; the hypothesis fails G6 and `budget_exhausted` ends the rounds | `test_no_surgeon_session_starts_past_the_budget` |
| L4 drill: drift mid-round → rebase → reverify record | ✅ main moves while the Surgeon works; the winner is re-verified merged onto the new main (never rebased, A-49) with the policy record `{G1, G2, G3, G4, G7}` | `test_l4_drift_mid_round_reverifies_the_winner_on_the_new_main` |

### Invariants and proof

- **I1**: nothing merges without G0–G6 and a readable green G7; run `…-232114` shows the
  refusal path (escalated, PR left unmerged).
- **I3**: every live run ended RESOLVED or ESCALATED with a legal transition log; a refused or
  over-budget Surgeon fails its hypothesis, never the incident.
- **I5**: the holdout is decrypted only inside G4 (Auditor) and G7 (Actions secret); outputs are
  aggregates; leak scans are zero.
- **L0, L1, L4**: the primary response, rewind with attempt refs kept, and drift re-verification.
- **C2–C5** pass on every attested commit; **C7**'s artifact (a green G7 run per merged PR)
  exists; **T2** (holdout, scoped key, fresh-seed G7) and **T5** (deterministic gates, blinded
  Auditor, Actions) are exercised.

### Known limits carried forward

- The Opus roles run on `claude-opus-5` until the headless CLI is updated (A-54); two G5
  judgements of the resolved run used the deterministic fallback after the subscription's
  session limit.
- The live rehearsals' round-1 failure came from G5 concerns; the gate-driven round failures
  (G3 flip, failing tests) are shown by the deterministic loop test with scripted Surgeons.
- The Fixes MCP prior (A4) is not connected (OAuth); antibodies, motif scans, vaccination,
  quarantine and the revert path are Stage 7 (the crew answers that none exist yet).
- The ledger is a file until Stage 8 moves it to the `ledger` branch; merge commits made by
  GitHub are committed by GitHub, so Stage 8's C2–C5 must verify the PR record for them.
- Stage 6 commit `fd102e6`'s checkpoint omits one test file from `files_touched` (A-58).

## Stage 5 — Control plane: Temporal + SQLite-WAL + Watchdog (D6)

Recorded 2026-10-11 (IST) on Windows 11 with temporalio 1.34.0, the SDK's time-skipping test
server and the Temporal CLI 1.9.1 dev server (Server 1.32.0). Spikes and drills:
`docs/SPIKES.md` (Stage 5); deviations: amendments A-40 to A-46.

### Targets

| Target | Result | Evidence |
|---|---|---|
| Time-skipping tests for every workflow | ✅ all 8 workflows (FleetSupervisor, Incident, Canary, CrossHostMaturation, VaccinationWave, Onboarding, SabotageCampaign, AuditAnchor) on the time-skipping server; durable timers are skipped, not slept (a canary interval, five hours of anchors) | `tests/control/test_workflows.py` (27 tests, about 21 s) |
| Replay-determinism tests | ✅ recorded histories of a resolved incident, a canary re-entry and an exhausted repair replay without error; an unpatched reordering (reproduce before triage) is caught by replay | `test_recorded_histories_replay_deterministically`, `test_an_unpatched_code_change_is_caught_by_replay` |
| `kill -9` drill, Temporal mode | ✅ live dev server and worker processes: after the worker's process tree is killed, the server records attempt 2 starting after a **HEARTBEAT** timeout; attempt 2 adopts attempt 1's commit by its nonce, or cleans up and commits under a new nonce; through the Watchdog, the chaos API kill → restart → adoption, with fresh pollers on every queue afterwards | `tests/contract/test_control_drill.py` (3 live tests, about 54 s) |
| `kill -9` drill, SQLite mode | ✅ the executor (with its session) and the single writer killed together; on restart the WAL-recovered log folds to REPAIRING, the dead lease is abandoned, attempt 2 adopts or cleans up | `test_reduced_mode_survives_kill9_of_the_whole_stack` (adopt, cleanup) |
| `continue_as_new` | ✅ FleetSupervisor continues every N events (7 events at 3 per run → 3 runs) carrying open incidents, waiting alerts, budgets and recent hit keys (a repeated key is still dropped); AuditAnchor continues daily | `test_the_supervisor_continues_as_new_and_keeps_its_state`, `test_the_audit_anchor_ticks_hourly_and_continues_as_new` |
| All signals and queries | ✅ `hit`, `drift`, `budget_update`, `pause_antibody`, `operator_pause`/`operator_resume`, `incident_closed`; Incident `drift_signal`, `attach`; VaccinationWave `pause`/`resume`; SabotageCampaign `request`/`close`; queries `status`, `open_incidents`, `budget`, `current_state`, `transition_log`, `progress`, `commitments` | supervisor, vaccination and sabotage tests |
| Activities with timeouts, heartbeats, retries and idempotency keys | ✅ the §5.3.2 table row for row (asserted), non-retryable `InvalidAlert`, `BudgetExceeded`, `ProtectedPathViolation`; heartbeats while the crew works; a replayed step returns its recorded result without redoing it; separate crew, surgeon and senses pools | `tests/control/test_activities.py` |
| **I3** (every incident terminates) | ✅ ten terminal scenarios (PARKED, every ESCALATED exit, RESOLVED, the secondary response) each fold to a legal `states.py` log; rounds and canary re-entries are bounded | `test_every_path_ends_terminal_with_a_legal_log`, `test_canary_re_entries_are_bounded` |
| **I1** ordering | ✅ ship runs only after the round's gates, once; a Coordinator naming a hypothesis that failed G0–G6 fails the workflow instead of shipping | `test_a_repaired_incident_resolves_through_every_stage`, `test_a_coordinator_cannot_ship_a_hypothesis_that_failed_its_gates` |
| L4 signal path | ✅ a `drift` signal mid-round triggers `rebase_and_rerun` before the next round | `test_a_drift_signal_mid_round_rebases_and_reruns_the_impacted_gates` |
| T8 (crash recovery not faked) | ✅ the server's own event history is the evidence (attempt 2 after a heartbeat timeout); `kintsugi control history` exports any workflow's history as JSON | `retried_after_heartbeat_timeout`; `tests/control/test_service.py` |
| Watchdog | ✅ restarts a pool at most 5 times in 10 minutes, then pages once; chaos API (bearer token, sabotage id required, Watchdog never a target, ingress outage with automatic recovery); health checks page once per failure; dead-man heartbeat | `tests/control/test_watchdog.py` |
| Senses → control plane | ✅ the queue forwarder turns claimed hits into `FleetSupervisor.hit` signals at least once (failed hand-offs are released and retried) and wakes the Watchdog on `ingestion_silent` for `kintsugi:*` | `test_forwarded_hits_reach_the_fleet_supervisor` |
| Spike N7 | ✅ `entire session resume` restores the session log; `claude -p --resume` continues the same session with its context | `docs/SPIKES.md` |

### Invariants and proof

- **I3**: the control plane enforces `states.py` at runtime: an illegal transition fails the
  workflow before it is recorded, in both Temporal and reduced mode.
- **I1** ordering: the Incident workflow ships only a winner whose G0–G6 verdict passed.
- **L2** (S-18): both modes recover from a `kill -9` with the §6.6 adoption rule; **T8**: the
  evidence is the server's event history.

### Known limits carried forward

- The crew is a port: Stage 6 binds the real Sentinel, Pathologist, Archivist, Coordinator,
  Surgeons and Auditor; Stage 5 runs the loop with scripted crews and the drill Surgeon.
- The Temporal dev server keeps history in memory; the 24-hour run needs the dev server's
  `--db-filename` (or Postgres), set up in WSL2 at Stage 13 (D2).
- The dead-man Lambda and the neatlogs `ingestion_health` rule are Stage 13 / A-38.

## Stage 4 — Senses: neatlogs, receptors, ingress, poller, Fixes

Recorded 2026-10-11 (IST) against the live neatlogs Public API (project tier `free`) with a
service-account token, neatlogs SDK 1.4.26, FastAPI 0.143.0 / uvicorn 0.54.0 and a cloudflared
2026.10.0 quick tunnel to the local ingress. Spike details: `docs/SPIKES.md` (Stage 4);
deviations: amendments A-28 to A-39.

### Targets

| Target | Result | Evidence |
|---|---|---|
| Contract tests against the pinned OpenAPI | ✅ 50 operations pinned from the published document (SHA-256 recorded); all 17 recorded live responses validate, mutated ones fail; live: the published document has **no drift** from the pin and live responses validate (4/4 contract tests) | `tests/senses/test_neatlogs_api.py`; `KINTSUGI_CONTRACT=1 … tests/contract/test_neatlogs_contract.py` |
| The Stage 3 `grader.verdict` spans are readable through the API (first contract test) | ✅ readable (`"PASS"`, JSON-encoded); **found** each verdict was its own one-span trace and fixed it, so a task's verdict and tool spans share `task:<id>` (A-28), verified live | `test_grader_verdicts_are_readable_through_the_api` |
| Duplicate events are dropped with 200 | ✅ a repeated event id answers 200 `duplicate`, enqueues and persists nothing; the dedupe holds 24 h | `tests/senses/test_ingress.py`, `test_event_dedupe_holds_for_24_hours_then_expires` |
| Ingress answers within 2 s | ✅ 200 signed deliveries: median 4.5 ms, p99 21.2 ms, max 68.5 ms (in-process); reachable through the tunnel (`/healthz`) | `test_ingress_answers_within_two_seconds` |
| Webhook verification | ✅ HMAC-SHA256 signature in the format neatlogs actually sends (spike A6) over the real delivery body, 256-bit secret path (404 otherwise), optional source allowlist, size cap, malformed bodies refused | `test_signature_secret_path_source_and_shape_are_enforced` |
| The poller recovers hits during a simulated outage | ✅ ingress at 503 while three hits arrive → the poller claims all three; after recovery the redelivered events claim nothing; restart from the persisted watermark recovers hits missed while down; an expired cursor restarts the sweep without double claims; concurrent claimers never double-process (150 hits × 4 threads) | `tests/senses/test_poller.py` |
| Replayed deliveries are suppressed | ✅ by event id (200 `duplicate`) and by hit key (a new event for an already-claimed hit claims nothing) | `test_a_replayed_delivery_is_suppressed_by_event_id_and_by_hit_key` |
| I7 completeness function on fixtures | ✅ passes when every hit in W opened or attached to an incident; fails on a missing or undecided hit; ignores hits outside W | `tests/senses/test_receptors.py` |
| I7 on live data | ✅ after one sweep: **281 hits, 0 missing**; all pending without a Sentinel (correctly FAIL); with stand-in dispositions in the local store: `281 hits -> 3 incidents + 278 dedupes (0 pending, 0 missing) PASS` | `kintsugi senses completeness --since 2026-10-11T15:30:00Z` |
| R-007 fires on Ledger `FAIL:` verdicts in a rehearsal (no sabotage committed) | ✅ three rehearsals (seeds 11, 12, 13) under the disclosed FX-outage profile: 18/18 tasks `FAIL:FX_UNAVAILABLE`; **every failing trace that reached neatlogs got an R-007 hit within seconds (15/15)**; the poller claimed them. One trace per run never appeared (A-39). R-001 Ledger also hit the FX 503 errors (10 new hits in the sweep after the third run) | `kintsugi senses rehearse`; `kintsugi/sandbox/profiles/rehearsal-fx-outage.yaml` |
| Receptors R-001…R-007 per host | ✅ provisioned live: 12 neatlogs detections (R-001, R-005, R-006, R-007 × 3 hosts) with per-event rules to the ingress webhook, and 9 Omega metric receptors (R-002/3/4 × 3); re-provisioning is idempotent and retires superseded versions and rules | `kintsugi senses provision`; `test_provisioning_is_idempotent_and_retires_stale_versions` |
| No false positives on the healthy fleet | ✅ R-002/R-003/R-004: 0 hits over 1,387 Ledger, 345 Scout and 576 Triage traces; R-006 v1 hit 137 healthy Ledger answers (`PHONE_NUMBER`), so v2 drops that entity for Ledger | `kintsugi senses poll --once`; spike per entity |
| Tag writes with optimistic concurrency | ✅ read-modify-write with `expectedRevision`, re-read on 409, no-op merges write nothing; live revision 0 → 1 | `tests/senses/test_records_and_tags.py` |
| Spikes A3, A5, A6, A7, A11, A13 (+ A12 probe) | ✅ recorded; A5 sharing cannot be enabled by API; A7 custom attributes are not readable; A13 rules can be created | `docs/SPIKES.md` |

### Invariants and proof

- **I7** (completeness): one shared `seen_hits` table claims each hit once across the webhook and
  the poller; the completeness function is the C9 primitive. The poller is the source of truth.
- **C8** primitive: `NeatlogsApi.trace(id)` reads a trace's existence and timestamps.
- **L0 detection** (R-007 on Ledger failures) and **L2** (poller recovery, replay suppression,
  the ingress's 503 outage switch) are exercised above.

### Known limits carried forward

- **Alerting (A-38):** after the first per-event delivery no alert rule in the project fired
  again; webhooks are therefore unproven beyond that delivery. The Architect is asked to check
  the neatlogs alerting UI or support. Omega pages from its own store (the Sentinel applies the
  per-host thresholds), so detection does not depend on it.
- **Ingestion loss (A-39):** about 3–4% of task traces never become visible; I7 can only cover
  what neatlogs reports. Investigation carries into Stage 5 and Stage 13.
- **OAuth-only operations (A-29):** delivery replay, webhook test, detection preview, generate
  and backfills need `NEATLOGS_OAUTH_TOKEN` (not configured); preview and backfills are also
  gated off in production.
- The webhook points at a cloudflared **quick tunnel** that ends with this session; rerun
  `kintsugi senses provision --public-url <origin>` against a stable origin before T0.
- R-005 is a `refusals` stand-in; conditional detections never fired, so R-002/3/4 run in Omega.
- The Fixes MCP is an interface with an unconfigured implementation (spike A4 is Stage 6).
- Two Stage 4 checkpoints under-report `files_touched` (A-37): commit 3
  (`01M4NRV5K0828Z1TWGGGYGREM4`, null) and commit 4 (`01M4NVAAQKHEQ58DCBE9HK0S46`, only
  `pyproject.toml`). Their trailers, refs and transcripts are intact; the cause was an edited
  file left uncommitted across commits of one turn.

## Stage 3 — Fleet: Ledger, Scout, Triage + sandbox and proxies

Recorded 2026-10-11 (IST) on Windows 11 + WSL2 Ubuntu 26.04, Docker Engine 29, gVisor
release-20261005.0, neatlogs SDK 1.4.26, anthropic SDK 1.13.0.

**Model mode: offline (amendment A-23).** No Anthropic key is configured yet, so every host
called its labelled offline stand-in through the LLM metering proxy. These baselines exercise
the whole host (tools, retries, chaos, sandbox-ready proxies, grading, telemetry); they are not
a measurement of Claude. Live baselines are re-run with `kintsugi fleet baseline` once
`ANTHROPIC_API_KEY` is in `.env`.

### Baselines (background chaos, seeds 1–3, train + sealed holdout)

| Host | SLO | Passed | Pass rate | Wilson 95% | p95 latency | Chaos absorbed | Health card |
|---|---|---|---|---|---|---|---|
| Ledger | ≥ 90%, p95 ≤ 25 s | 360 / 360 | 1.000 | [0.989, 1.000] | 0.42 s | 102 `fx_rate` 429s in 1,149 calls | `evidence/baselines/ledger.json` |
| Scout | ≥ 85% | 180 / 180 | 1.000 | [0.979, 1.000] | 0.55 s | 77 `fetch_document` 429s in 1,986 calls | `evidence/baselines/scout.json` |
| Triage | ≥ 88% | 282 / 300 | 0.940 | [0.907, 0.962] | 0.33 s | — (no GitHub faults in the background profile) | `evidence/baselines/triage.json` |

Per seed: Ledger 120/120 ×3, Scout 60/60 ×3, Triage 94/100 ×3. Triage's 18 failures are the
Haiku stand-in's declared blind spot (A-27): issues with Japanese titles (12 `WRONG_COMPONENT`,
6 `WRONG_TYPE`). Metered cost (list price, non-billable): Ledger $0.0428, Scout $0.0120, Triage
$0.0266.

### Targets

| Target | Result | Evidence |
|---|---|---|
| Baselines at 3 seeds ≥ SLO with Wilson CIs | ✅ all three hosts (offline model) | table above; `kintsugi fleet baseline` |
| Ledger handles the 8% background 429s with **no duplicate journal lines** | ✅ 102 injected 429s, zero `FAIL:DUPLICATE_ENTRY` across 360 tasks | `evidence/baselines/ledger.json` |
| Ledger keeps the S-17 target shape | ✅ live graph: `run_reconcile` calls `retry_with_backoff` and contains `attempt`; `ReconcileSession` (mutable accumulator) is constructed by `run_reconcile`, not `attempt`; `ReconcileSession.post` reaches the side-effecting `post_journal_entry`. Adding only `attempt -CONSTRUCTS-> ReconcileSession` and the call it types completes motif v1.1 | `tests/contract/test_fleet_motifs.py` |
| The healthy fleet has no motif false positive; Scout is the disclosed look-alike | ✅ v1.1 matches no host; v1.0 matches exactly Scout (`fetch_all_pages` / `attempt` / `ScratchBuffer`) | `tests/contract/test_fleet_motifs.py` |
| Deterministic cassette replay | ✅ Ledger, Scout and Triage: replayed verdicts equal the recorded ones on 3/3 replays; the cassette carries model answers and keeps injected 429s at their positions; an unseen request fails the task instead of calling out | `tests/fleet/test_replay.py` |
| Chaos proxy identical per seed | ✅ same faults at the same (task, occurrence) under three different thread interleavings; a different seed differs; an injected status never reaches a side-effecting upstream | `tests/sandbox/test_chaos_proxy.py` |
| LLM proxy enforces per-host budgets and the daily cap | ✅ worst-case reservation in one SQLite transaction: 60 concurrent $0.50 reservations against a $20 cap grant exactly 40; refusals are HTTP 402 and nothing is forwarded; the cap resets per UTC day; offline traffic is metered but never consumes the cap | `tests/sandbox/test_llm_proxy.py` |
| LLM proxy meters cost within 1% | ✅ metered = list-price cost of the reported usage (cache writes 1.25×/2×, cache reads, Haiku 5.5 long-context tier), including streamed `message_delta` usage; the real Anthropic SDK works through it (create and stream) | `tests/sandbox/test_llm_proxy.py`, `tests/econ/test_prices.py` |
| Sandbox blocks non-allowlisted egress (spike N8) | ✅ under `runsc` as uid 65534 with a read-only root, only the gateway's allowlisted route answers; direct IPs, DNS, the bridge and unlisted routes fail; the wall-clock limit kills the container | `tests/contract/test_sandbox_contract.py`; `docs/SPIKES.md` |
| `neatlogs doctor --local --json` and `--probe --json` pass | ✅ local: `local_envelope`; probe: visibility, finalization, hierarchy, attributes, input/output, metadata, typed tokens | run 2026-10-11 with the `.env` key passed to the child only |
| `grader.verdict` spans | ✅ every graded task emits `grader.verdict` with `PASS` / `FAIL:<reason>` (`hostkit.telemetry`); baselines exported with the SDK key. Reading them back through the Public API is the Stage 4 client's first contract test | `hosts/hostkit/hostkit/telemetry.py` |
| Holdout unreadable without the key; canary strings; manifest matches | ✅ age-encrypted to `.kintsugi/holdout.pub`; the canary is in every holdout task and absent from the ciphertext; a wrong identity fails; `kintsugi holdout verify` checks `.kintsugi/holdout.sha256` and catches a one-byte change | `tests/fleet/test_fleet.py`, `tests/test_cli.py` |
| Carried model-tier spikes (A-22) | ✅ A1/N2, N1 headless, N5 linkage, A14 mid-tool kill: 4 passed | `docs/SPIKES.md` |
| Fixture-repo motif tests keep passing | ✅ unchanged | `tests/memory/test_graph.py`, `tests/contract/test_graph_contract.py` |

### Invariants touched

- **I4** (spend within budget): the proxy refuses any call whose worst case would breach the
  $20/day cap or the host's monthly LLM budget, atomically across threads and processes.
- **I5** (holdout never in crew context): holdouts are generated from a secret seed, encrypted,
  canaried and decrypted only by Omega to be piped into a host process on stdin.

### Known limits carried forward

- All baselines are `model=offline` (A-23). Live baselines need `ANTHROPIC_API_KEY`; Bedrock is
  `NOT_AUTHORIZED` for Claude in the account (AWS case 179171381300975).
- Holdouts are sealed to a rehearsal identity; the T0 checklist re-seals them under Architect
  custody (A-26).
- `g7-gates.yml` is the Stage 3 skeleton (G1, G3, manifest, G4 fresh-seed run); G2 and G4's
  paired statistics arrive with incidents in Stage 6.
- The runtime (`kintsugi fleet up`) is tested in accelerated runs; the 24-hour run starts from
  WSL2 at T0, never from inside the desktop app (D2).
- Scratch observations, deliberately not committed so the sabotage mechanism is not seeded into
  the repository: Ledger with the session built inside `attempt` fell to 82–88% with
  `FAIL:DUPLICATE_ENTRY` under the same chaos; Scout with the buffer hoisted out of `attempt`
  failed 19/200 tasks with `FAIL:DUPLICATE_CITATION` while the intended code passed 200/200.
