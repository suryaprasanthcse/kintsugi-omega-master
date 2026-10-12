# KINTSUGI‑Ω Execution & Build Plan

| Field | Value |
|---|---|
| Status | **Approved by the Architect** (Surya Prasanth R, @suryaprasanthcse), 2026‑10‑11 |
| Specification | `../Blueprint_Omega_Master.md` (v1.0, full version). This plan reconciles it with the verified environment; it does not dilute it. |
| Visual archive | `../kintsugi-omega-blueprints/kintsugi-omega-blueprint-img_{1..12}.png` |
| Scope | Full Blueprint Omega. No reduced cut. |

This file is the in-tree source of truth for every interactive or headless session. Amendments discovered during execution are appended to the **Amendments log** at the end, never edited into the approved text.

---

## 0. Schedule constraint

- Plan approval: 12:27 IST, Oct 11. Submission window closes 23:55 IST, Oct 12 (about 35½ h).
- The specification itself estimates about 4 engineer-weeks for the full build (§13.2 R1, "Certain"). The estimate for Stages 1–13 below is 45–50 h of continuous wall-clock work.
- The autonomy window W must end by about 19:55 IST Oct 12 to leave about 4 h for reveals, audit, video and submission. Therefore **T0 ≤ 19:55 IST Oct 12 − W**.
- W and T0 are decided at the Stage 8 go/no-go gate (ruling D1).

## 1. Verified environment baseline (pre-Stage 1)

| Area | Verified state |
|---|---|
| Entire | CLI 0.11.4 and entire-graph v0.4.0 (`no_egress: true`). Checkpoints are per-checkpoint refs `refs/entire/checkpoints/<last-2-chars>/<ULID>`, not the legacy `entire/checkpoints/v1` branch. No `rewind` command exists. Available: `entire checkpoint explain --json/--transcript`, `entire why <file>:<line> --json`, `entire session resume/stop`. |
| Entire behaviour | A mid-turn commit is linked only when the files were edited with Claude Code's Edit/Write tools. Each Stop hook adds a "Finalize transcript" commit, which moves the checkpoint ref. The pre-push hook only pushes refs in its local queue. Built-in redaction blanks secret-looking lines even with zero configured rules. |
| Windows | Entire's `prepare-commit-msg` blocks on a hidden `CONIN$` prompt unless `ENTIRE_TEST_TTY=0`. The Claude desktop app is MSIX-packaged and virtualizes `%APPDATA%`/`%LOCALAPPDATA%` writes, so tooling lives under `~/.local/share` (`UV_PYTHON_INSTALL_DIR`, `ENTIRE_PLUGIN_DIR`). |
| Tools present | `claude` 2.1.269 (headless CLI; the desktop app runs 2.1.295), Node, npm, git 2.55. Docker Desktop installed, engine stopped. WSL2 distros Ubuntu and docker-desktop (stopped). |
| Tools missing | `temporal`, `gh`, `make` (Windows), `age` CLI (the `pyrage` library replaces it). |
| Python | uv-managed CPython 3.12.14, pinned by `.python-version`. |
| Commit policy | Sole author and committer: Surya Prasanth R `<suryaprasanthcse@gmail.com>`. Never `Co-authored-by:`. Attribution only through `Entire-Checkpoint:` and `Kintsugi-*` trailers. |

## 2. Spec-to-reality reconciliation

### (a) No `rewind`: L1, L2, L3

- **L1 (failed hypotheses).** Rewind means *discard the worktree, keep the checkpoint* (spec §4.3.4, honest scope). Every attempt therefore commits, including attempts that will fail the gates, because a checkpoint exists only for a commit. The Surgeon session edits `wt/INC/Hk`; after the session ends the orchestrator signs and commits on `kintsugi/INC/Hk`; Entire links the commit to the session. The attempt is pinned as `refs/kintsugi/attempts/INC/Hk/<n>` and pushed with the checkpoint refs; then `git worktree remove --force`. Attempt commits stay reachable (never garbage-collected) and audit checks C2–C5 can verify them.
- **L2 (process killed mid-session).** A headless session killed before its Stop hook has no shadow branch and no checkpoint (A14). Recovery: adopt a commit whose nonce matches the ledger; otherwise kill the process group, `entire session stop <id>`, `git reset --hard` + `git clean -fdx` inside the disposable worktree only, hash the Claude Code transcript (`~/.claude/projects/.../<sid>.jsonl`) into the bundle as evidence of the abandoned attempt, write an `abandoned` ledger entry, and start attempt 2 with a **new nonce**. Long-lived in-place sessions (Immunologist refinement, orphan recovery) use `entire session resume <branch>` plus `claude -p --resume <sid>` (spike N7).
- **L3 (wrong fix in production).** Reverting is git (`git revert -m 1` → PR → expedited gates → merge), not Entire. Because audit check C4 requires a checkpoint for **every** commit in W, reverts, L5 configuration treatments and eval-augment commits run inside a templated **headless notary session**: a Claude Code session whose prompt contains the nonce and performs exactly that operation.

### (b) Audit check C4 and the Archivist's Intent Record

**C4/C5**, for every commit in W on protected branches plus attempt refs:

1. Require both the `Kintsugi-*` trailers and an `Entire-Checkpoint:` trailer.
2. The ref `refs/entire/checkpoints/<id[-2:]>/<id>` must exist on origin. Offline mode reads a `git bundle` of these refs.
3. `entire checkpoint explain <id> --json`: `files_touched` must cover the files in the diff.
4. `entire checkpoint explain <id> --transcript`: the nonce must appear verbatim and not on a redacted line.
5. Cross-check: `entire why <file>:<line> --json` on sampled changed lines names the same checkpoint and commit.
6. **C5:** the session's first user message, canonicalised and SHA-256'd, equals `Kintsugi-Prompt`.

**Intent Record:** `entire graph search` → `git blame` → resolve the checkpoint two independent ways (`entire why --json` and the commit trailer; they must agree) → read the transcript → select the turn(s) whose Edit/Write tool calls touched the blamed file (the "Intent" field is ignored: it shows the first prompt in the checkpoint's scope) → the Archivist (Opus) writes clauses with verbatim quotes → the Auditor checks every quote against the same transcript bytes, whose SHA-256 is recorded. `git bisect` steps map to checkpoints through trailers; commits without one are labelled `no-checkpoint`. The honeytoken check (§7.2) runs `git grep` over **all** `refs/entire/checkpoints/*` trees.

### (c) Motif engine on entire-graph v0.4.0 (Python)

Python relations: `CALLS`, `ASYNC_CALLS`, `CONSTRUCTS`, `CONTAINS`, `DATA_FLOWS`, `DEFINES`, `IMPORTS`, `EXTENDS`, `INHERITS`, `OVERRIDES`, `PARAM_TYPE`, `RETURNS_TYPE`, `USES_TYPE`, `HANDLES_GRAPHQL`, plus the heuristic relations of the `full` profile (`HTTP_CALLS`, `HANDLES_TOOL`, `TESTS`, `FILE_CHANGES_WITH`). There is no `READS_FIELD`, `WRITES_FIELD`, `ACCESSES` or `IMPLEMENTS` for Python.

- **Motif v1.0:** nodes `R[is_retry_wrapper]`, `Enc` (enclosing function), `F` (the retried scope: nested function, lambda, or the decorated function itself), `S[has_mutable_accumulator]`; edges `[Enc,R,CALLS]`, `[Enc,F,CONTAINS]`, **`[F,S,CONSTRUCTS]`**.
- **Motif v1.1** adds `T[is_side_effecting]` and `[F,T,CALLS*]`, where `CALLS*` is a derived transitive-closure relation computed before VF2 matching (VF2 matches edges exactly).
- **`has_mutable_accumulator`:** `entire graph def S` (fields and methods) plus a deterministic stdlib `ast` scan for list/dict/set fields mutated in methods; Haiku decides only when the scan is inconclusive, and its verdict is cached by (symbol, blob SHA).
- **`is_side_effecting`:** `CALLS*` reaches a tool annotated `side_effect: true` in the host's tool registry, or an `HTTP_CALLS` POST/PUT/DELETE.
- Validation is unchanged: match before the fix, no match after it, precision ≥ 0.9 on the healthy fleet. The snapshot record schema is pinned with golden files (A9).

### (d) `ENTIRE_TEST_TTY=0` everywhere

- Build commits: `.claude/settings.json` `env` block and the CLAUDE.md commit rules.
- Runtime: one subprocess gateway, `kintsugi/sandbox/proc.py`, builds every child environment from an allowlist and forces `ENTIRE_TEST_TTY=0`, `GIT_TERMINAL_PROMPT=0`, `GIT_EDITOR=true`, with timeouts and process-tree termination (root first, so a killed `git commit` aborts instead of completing without a trailer). An architecture test and a ruff banned-API rule forbid `subprocess` anywhere else.
- Headless sessions get the environment from the gateway plus `--settings` (which also sets commit attribution to `""`).
- Every commit is verified afterwards (trailer + checkpoint ref). On failure the activity fails, the disposable worktree is reset and nothing is pushed.
- Entire 0.11.4 and entire-graph 0.4.0 are pinned; a contract test runs the hook on a scratch repo under a timeout to catch regressions.

### (e) Further reconciliations

1. **Redaction vs. the proofs (critical, spike N1).** Entire redacts secret-looking lines. A hex nonce or random-looking honeytoken could be blanked: C4 would fail and, worse, C6's "0 honeytoken hits" would pass trivially. Nonces and honeytokens are therefore **passphrase-formatted**; Stage 2 adds a byte-exact prompt round-trip test and a **positive control** proving C6 finds a planted honeytoken.
2. **Parallel Surgeons (N5).** K concurrent sessions in K worktrees: each commit must link to its own session.
3. **Ref syncing.** Checkpoint refs are pushed explicitly after each commit and again after finalize; audit clones fetch `refs/entire/*`.
4. **Public transcripts.** Checkpoint transcripts are pushed with the code: add redaction rules for Anthropic and neatlogs key patterns and a secret scan of checkpoint trees before every push (N6).
5. **Platform.** gVisor (§5.10) needs Linux: the runtime for Stage 3+ and the autonomy run lives in WSL2 Ubuntu (ruling D2). The 24 h run is never launched from inside the Claude desktop app.
6. **No co-author trailers.** The orchestrator's commit validator rejects `Co-authored-by:`; headless sessions get commit attribution set to `""`.

## 3. Architect rulings D1–D8

| # | Decision | Ruling |
|---|---|---|
| D1 | W length and T0 | Deferred to the Stage 8 go/no-go gate, once the C1–C14 mutation checks are green. |
| D2 | Runtime platform | Stages 1–2 stay cross-platform through `kintsugi/sandbox/proc.py` so local Windows execution works. Prepare WSL2 Ubuntu + Docker/gVisor for Stage 3+ and the autonomy run. |
| D3 | Repo layout | Build the hosts (`hosts/ledger`, `hosts/scout`, `hosts/triage`), the antibody library (`antibodies/`) and the sabotage log (`sabotage-log/`) inside the `kintsugi-omega` monorepo first, so `suryaprasanthcse/kintsugi-omega` is self-contained and runnable from a single clone. Remote repo adapters are configurable once the GitHub token is provided. |
| D4 | Commit identity | `Surya Prasanth R <suryaprasanthcse@gmail.com>` is the sole git author and committer on all commits. Never a bot contributor, never `Co-authored-by:`. Machine origin is proven by Ed25519 signatures (`.kintsugi/attest.pub`), `Entire-Checkpoint:` trailers and `Kintsugi-*` trailers. |
| D5 | Ledger location | An unprotected, append-only `ledger` branch for ledger entries and anchors. |
| D6 | Temporal deployment | Implement both the deterministic `sqlite_fsm.py` WAL state machine and the Temporal workflow/activity definitions, using the single-binary Temporal CLI dev server or Docker Compose when started in Stage 5. |
| D7 | Headless Claude billing | The existing Claude Code login for Stage 2 headless spikes; `ANTHROPIC_API_KEY` with a hard daily dollar cap in `kintsugi/sandbox/llm_proxy.py` and `kintsugi/econ/governor.py` from Stage 3/6. |
| D8 | Parallel build lanes | Approved where safe, provided every commit is made cleanly with `ENTIRE_TEST_TTY=0`, carries a verified `Entire-Checkpoint:` trailer, and credits lines accurately. |

## 4. Stages

"Rehearsal" means an unsealed, clearly labelled practice run before T0.

### Stage 1 — Foundation and deterministic cores (no credentials)

- **Builds:** `pyproject.toml`/`uv.lock`, ruff, pyright, `Makefile` (judge, audit, demo, fleet-up/down, check), `.github/workflows/omega-ci.yml`, `kintsugi/cli.py`, `kintsugi/sandbox/proc.py`, `kintsugi/proof/{canonical,ledger,sealed,attest,bundle}.py`, `kintsugi/stats/{mcnemar,sprt,power,holm,wilson,bootstrap}.py`, `kintsugi/econ/{governor,wright}.py`, `kintsugi/memory/{antibody,motif}.py`, `kintsugi/control/states.py`, `schemas/E1–E6`, `.kintsugi/protected_paths.yaml`.
- **Commits (~7):** scaffold + plan · proc gateway · proof primitives · stats · econ · memory/motif core · state machine, schemas, protected paths.
- **Verified by:** SPRT boundaries 2.890/−2.251, steps +0.6931/−0.0660, accept after 35 clean runs, reject at n=8 (LLR 3.268), accept at n=46 (LLR −2.275); Connor n=34; McNemar b=26, c=1 → p≈2.09e-7; governor reproduces §6.2 (Pool $71.70, Cap/B $12.40) and §6.8 (B $4.83, T1 $2.34, GM 78.6%); Wright fit recovers b; RFC 8785 vectors; ledger tamper detected at the exact seq; commit-reveal and Ed25519 round-trip; property test: every state path terminates. Spike N3 is checked on the first commit.
- **Invariants:** model-level I3; I4 formulas; I1/I2/I7 primitives. **Cascade/proof:** SPRT core (L3), L5 math, building blocks for C1, C3, C6.

### Stage 2 — Entire, graph, git and attestation layer + headless spikes

- **Builds:** `kintsugi/memory/{entire,graph}.py` (every subcommand, JSON adapters, golden fixtures), `kintsugi/sandbox/worktree.py`, `kintsugi/crew/claude_session.py` (`claude -p --output-format stream-json --allowedTools … --settings …`, nonce injection, cost and transcript capture), `kintsugi/proof/{commit,redaction}.py`, `tests/contract/test_entire_*.py`.
- **Spikes (contract tests on scratch repos):** A1/N2 (does Entire link a commit made after `claude -p` exits?), N1 (redaction vs nonce/honeytoken/prompt formats), N5 (three concurrent worktrees), A14 (`kill -9` mid-session), N4 (hooks inside worktrees), A9 (snapshot schema).
- **Commits:** ~6. **Verified by:** C4/C5 pass on real checkpoints and fail on mutated ones; `entire why` agrees with the trailer mapping; the C6 positive control finds a planted honeytoken.
- **Invariants:** I2 machinery; I6 detectability; I5 transcript-scan tooling. **Cascade/proof:** L1 attempt checkpoints, L2 abandoned-session handling, C2–C6.

### Stage 3 — Fleet: Ledger, Scout, Triage + sandbox and proxies (monorepo, D3)

- **Builds:** `hosts/ledger` (`ReconcileSession`, `run_reconcile`, `util/retry.py`, tools `parse_invoice`, `lookup_vendor` with a default argument, `fx_rate`, side-effecting `post_journal_entry`, `fmt_amount`; runtime with auto-deploy from main; 120-invoice generator 80/40; exact-match grader; `test_resume_after_partial_batch`), `hosts/scout` (BM25 frozen corpus; the disclosed look-alike per-attempt scratch buffer; 40/20), `hosts/triage` (mock GitHub, `config/routing.yaml`, mixed-language issues; 70/30), `kintsugi/sandbox/{runner,llm_proxy,chaos_proxy,cassette}.py`, age-encrypted holdouts, `.kintsugi/holdout.sha256`, per-host CI and a reusable G7 workflow, `grader.verdict` spans. WSL2 + Docker + gVisor integration (D2). Remote repo adapters stay configurable (D3).
- **Commits:** ~12. **Verified by:** baselines at 3 seeds ≥ SLO with Wilson CI; Ledger handles 8% background 429s with no duplicate lines; deterministic cassette replay; chaos proxy identical per seed; proxy enforces budgets and meters cost within 1%; sandbox blocks non-allowlisted egress; `neatlogs doctor --probe`; holdout canary strings for leak scans.
- **Invariants:** I5 (encryption), I4 (proxy caps). **Cascade/proof:** code shapes for S‑17 (L0), S‑19 targets (L4), the Scout look-alike (L3), Triage Pilot on Haiku (L5).

### Stage 4 — Senses: neatlogs, receptors, ingress, poller, Fixes

- **Builds:** `kintsugi/senses/{neatlogs_api,detections,ingress,poller,tags,fixes_mcp}.py`, receptors R‑001…R‑007 per host, SQLite dedupe store, webhook registration. **Spikes:** A3–A7, A11–A13.
- **Commits:** ~6. **Verified by:** contract tests against the pinned OpenAPI; duplicate events dropped with 200; ingress answers within 2 s; poller recovers hits during a simulated outage; replayed deliveries suppressed; I7 completeness function on fixtures.
- **Invariants:** I7. **Cascade/proof:** L0 detection, L2 poller and replay, C8, C9.

### Stage 5 — Control plane: Temporal + SQLite-WAL + Watchdog (D6)

- **Builds:** `kintsugi/control/workflows.py` (8 workflows), `activities.py` (all §5.3.2 activities with timeouts, heartbeats, retries, idempotency keys), `worker.py` (separate crew, Surgeon, senses pools), `sqlite_fsm.py` (§5.4 resume algorithm, single-writer socket), `watchdog.py` (supervision, health checks, authenticated chaos API, dead-man switch).
- **Commits:** ~6. **Verified by:** time-skipping tests for every workflow; replay-determinism tests; `kill -9` drill (heartbeat timeout → attempt 2 adopts by nonce or cleans up with a new nonce) in both Temporal and SQLite modes; `continue_as_new`; all signals and queries.
- **Invariants:** I3, I1 ordering. **Cascade/proof:** L2, L4 signal path, T8.

### Stage 6 — Crew, primary incident loop, gates G0–G7, ship (L0, L1, L4)

- **Builds:** `kintsugi/crew/{sentinel,pathologist,archivist,coordinator,surgeon,auditor}.py` with hashed prompt templates, `kintsugi/senses/{signature,drift}.py`, `kintsugi/gates/g0…g7`, context manifests, `kintsugi/control/ship.py` (issues, PRs with the E.4 receipt, merges), the protected-path pre-commit hook, `.github/workflows/g7-gates.yml` (holdout key in an Actions secret; logs print aggregates only).
- **Commits:** ~12. **Verified by a full Ledger rehearsal:** G0 fails 3/3 → Intent Record with verified quotes → three parallel Surgeons, each with a checkpoint → round 1 fails and is rewound → round 2 → pre-registered dispute experiment → ship → G7 green → merge. **Negative tests:** protected-path edits blocked twice; diffs over 120 lines blocked; new outbound network calls blocked; zero holdout leaks across crew spans and transcripts; the Auditor never sees Surgeon output; G6 stops a round when the budget is spent. **L4 drill:** a mid-round drift push → rebase → reverify record.
- **Invariants:** I1, I3, I5. **Cascade/proof:** L0, L1, L4; C2–C5, C7; T2, T5.

### Stage 7 — Canary, antibodies, motif engine, vaccination, quarantine (L3)

- **Builds:** `kintsugi/gates/canary.py`, `kintsugi/control/revert.py`, `kintsugi/memory/{motif,predicates,library,contrastive}.py`, `kintsugi/crew/immunologist.py` (synthesis, receptor generate/preview/create/activate, backfill, maturation, vaccination, quarantine, refinement), the `antibodies/` library in the monorepo (D3), the trace-to-test eval-augment path.
- **Commits:** ~10. **Verified by:** motif validation (match before, not after, precision ≥ 0.9); induced-trigger rule; receptor preview precision ≥ 0.9; a backfill job completes; L3 rehearsal (Scout canary reject → revert PR → reverse canary → `pause_antibody` → comments on open PRs → QUARANTINED with the receptor still active); contrastive learner separates 3 positives from Scout; v1.1 motif excludes Scout; consistent lineage; secondary responses measured far cheaper than primary.
- **Cascade/proof:** L3, L0 steps 11–14, S5, C11, T9.

### Stage 8 — Proof plane: Saboteur, sealing, ledger anchors, `kintsugi audit`

- **Builds:** `kintsugi/crew/saboteur.py` (catalog enums, strict-schema generation, realism rules + rubric ≥ 0.7, sealing, age storage, upstream-lane delivery, chaos API), `kintsugi/proof/{sealed,ledger anchors,attest(KMS option),bundle,audit}.py`, `sabotage-log/` in the monorepo (D3), anchors on the `ledger` branch (D5), `make judge`.
- **Commits:** ~10. **Verified by a mutation suite:** every check C1–C14 passes on a clean fixture and fails on a tampered one (reordered ledger, missing trailer, forged signature, missing nonce, prompt mismatch, honeytoken positive control, missing G7 run, unknown trace ID, unmatched hit, cost Δ over 1%, lineage gap, ruleset gap, changed holdout hash, protected self-repair path touched). Offline mode needs no credentials; exit codes 0/1/2.
- **Invariants:** I2, I6, I7. **Cascade/proof:** sealing S‑17/18/19; T1, T3, T4, T8, T10. **Go/no-go gate for D1.**

### Stage 9 — Economic cortex and cfo.ai (L5)

- **Builds:** `kintsugi/econ/{governor (live), actuals, wright (nightly refit), cfo_sync}.py`, `business/{plan.json, actuals.json, actuals.csv, cfo_export/}`, the L5 treatment path with its decision span.
- **Commits:** ~5. **Verified by:** plan hash pinned per incident; C10 within 1%; the governor never lowers power below 0.8 and escalates instead; L5 rehearsal on Triage; cfo.ai model with Unit economics / Runway / Scenarios tabs and plan-vs-actual.
- **Invariants:** I4. **Cascade/proof:** L5, T7.

### Stage 10 — Onboarding O1–O7, Repair Clinic, open-source hosts

- **Builds:** `kintsugi/onboard/{intake,instrument,eval_synth,baseline,immune_scan,replay_mode}.py`, the Onboarding workflow, a Clinic intake issue form with consent checkboxes, PR-only receipts for foreign repos.
- **Commits:** ~6. **Verified by:** OSS‑1 onboarded end to end in the sandbox; replay mode from exported traces; code refuses to merge into a foreign repo.
- **Cascade/proof:** maturation and vaccination targets, T9, building in public.

### Stage 11 — Arena and public API

- **Builds:** `arena/` (Next.js: fleet board, incident stream, immunity matrix, learning curve, ledger head, "Sabotage it"), `kintsugi/arena_api.py` (strict-schema Haiku compiler, sealing, immediate commitment), rate limiting, daily cap.
- **Commits:** ~6. **Verified by:** injection fuzz corpus compiles only to catalog values or is rejected; 1 request per IP per 10 minutes; escalations displayed.
- **Cascade/proof:** T6, demo, building in public.

### Stage 12 — Self-repair (INC‑SELF‑01)

- **Builds:** `hosts/omega` (replay suite of 20 incidents recorded in Stages 6–9 rehearsals), grader (root-cause hit@3, cost, p95), enforcement of the self-repair protected scope (C14).
- **Commits:** ~4. **Verified by:** a self-repair rehearsal with before/after trace sets; C14 blocks any diff touching proof, stats, gates or attestation code.
- **Cascade/proof:** §6.9, the portal's "agent improved" row.

### Stage 13 — Integration, chaos drills, dress rehearsal, T0 preconditions

- Compressed L0–L5 + self-repair dress rehearsal; `kintsugi audit --all` green.
- T0 checklist (§6.1): baselines recorded; background chaos profile on; holdout hashes committed; attestation key generated under Architect custody, public key committed to `.kintsugi/attest.pub` and posted; genesis hash; rulesets active; S‑17/18/19 sealed; runtime outside the Claude desktop app with keep-awake.
- Omega's own code is frozen at T0; after that only Omega's self-repair loop may change it.

### Stage 14 — Autonomy window W, then submission

- During W: hourly anchors (human-posted); Clinic tracked.
- After W: reveals, `kintsugi audit --all`, published bundles, `make judge`, the §8 video recorded from the real run, the X quote between 7:00 and 11:55 PM IST on Oct 12.

### Coverage map

| Item | Stages |
|---|---|
| L0 | 3, 4, 6, 7, 8 |
| L1 | 2, 6 |
| L2 | 4, 5, 8 |
| L3 | 1, 7 |
| L4 | 3, 6 |
| L5 | 1, 9 |
| Self-repair | 12 |
| C1–C14 | 8 (built from Stages 1–7) |
| I1 | 5, 6 |
| I2 | 2, 8 |
| I3 | 1, 5 |
| I4 | 3, 9 |
| I5 | 3, 6 |
| I6 | 2, 8 |
| I7 | 4, 8 |

## 5. Credentials, decisions and spikes

### Credentials and accounts

| Input | Needed by | Can proceed without it |
|---|---|---|
| `NEATLOGS_API_KEY` (SDK) | Stage 3 | Host code and offline tests |
| neatlogs service-account token + project UUID (Public API) | Stage 4 | Client against the pinned OpenAPI with fixtures |
| neatlogs alert rules in the UI (A13) + public ingress URL | Stage 4 | Poller path |
| neatlogs Fixes MCP (A4) | Stage 6 | Loop runs without the prior |
| `ANTHROPIC_API_KEY` with a spend cap | Stage 3 (proxy), Stage 6 (crew) | Stage 2 spikes use the existing Claude login (D7) |
| GitHub token or App; `gh` installed | Stage 3 (remote adapters), Stage 6 (ship), Stage 13 (rulesets) | Stages 1–2 |
| Signing: AWS KMS `ECC_NIST_P256` + CloudTrail, or local Ed25519 | Stage 8 | Ed25519 from Stage 1 |
| cfo.ai account + model + Ari MCP (A8) | Stage 9 | Governor math |
| AWS (S3 object lock, Fargate, SQS, dead-man Lambda), optional | Stages 4/8/13 | Local equivalents |
| Docker engine in WSL2 Ubuntu + gVisor; `temporal` CLI; Node | Stages 3, 5, 11 | — |
| Crustdata key (optional) | Stage 3 | Optional |

### Spikes

| Spike | Stage | Status at approval |
|---|---|---|
| A1/N2 headless hooks and post-exit commit linking | 2 | Open |
| A2 checkpoint data readable programmatically | — | Resolved |
| A10 graph supports Python semantically | — | Resolved |
| A3, A5, A6, A7, A11, A13 (neatlogs behaviour) | 4 | Open |
| A4 Fixes MCP | 6 | Open |
| A8 cfo.ai MCP | 9 | Open |
| A9 snapshot schema | 2/3 | Open |
| A12 backfill bounds | 7 | Open |
| A14 abandoned sessions | 2 | Open |
| A15 RFC 8785 library | 1 | Open |
| N1 redaction vs nonce/honeytoken/prompt (critical) | 2 | Open |
| N3 stale `last_checkpoint_commit_hash` after the history rewrite | 1 | Open |
| N4 hooks in worktrees | 2 | Open |
| N5 concurrent sessions in parallel worktrees | 2 | Open |
| N6 secret scan of public transcripts | 2 | Open |
| N7 `entire session resume` + `claude --resume` | 5 | Open |

Stage 2 results are recorded in `docs/SPIKES.md` (A9, N1, N4, N5 and A14 mechanics resolved; the
model tier of A1/N2, N1, N5 and A14 waits on A-20).
| N8 gVisor in WSL2 | 3 | Open |
| N9 rulesets and merge identity | 6/13 | Open |

### Do now, independent of code

Repair Clinic intake starts now (§15.2): third-party merges need lead time and are the strongest proof (★★★★★). Posting is the Architect's; the intake form lands in Stage 10.

---

## Amendments log

Entries are appended in order as execution discovers them. Each states what changed and why.

| # | Stage | Amendment | Why |
|---|---|---|---|
| A-1 | 1 | Entire's line attribution is binary: lines from Claude Code's Edit/Write tools are `agent_lines`; everything else, including tool-generated files such as `uv.lock`, is `human_added`. Lockfiles are always produced by tooling, never hand-written. | Commit 1 showed 511 agent + 595 "human" lines; the 595 are exactly `uv.lock`. The authorship claim rests on attestation and checkpoint linkage, not on Entire's agent percentage. |
| A-2 | 1 | The attestation digest realises `‖` as the RFC 8785 encoding of the six fields as a JSON array. | Plain concatenation is ambiguous at field boundaries (`"ab"‖"c"` = `"a"‖"bc"`). |
| A-3 | 1 | Nonces are 128-bit, 11-word passphrases; honeytokens are 72-bit, 6-word passphrases (`kintsugi/proof/passphrase.py`). Hex nonces are refused. The spec's E.6 hex example is superseded. | Spike N1 format. Survival through Entire's redaction is verified in Stage 2. |
| A-4 | 1 | The T0 attestation key is generated by the Architect with `kintsugi keys generate` outside any repository; only `.kintsugi/attest.pub` is committed, at T0 (Stage 13). No key exists in the repo before then. | Key custody (§4.5.4): the private key must never enter a repository or an agent's context. |
| A-5 | 1 | Four I3-closure transitions extend the §5.4 table: REPRODUCED→ESCALATED (no suspect), ATTRIBUTED→ESCALATED (zero budget), SHIPPED→ESCALATED (merge blocked), CANARY→ESCALATED (revert failed or re-entry limit). Canary re-entries are bounded (default 2). | Without them an incident can stop in a non-terminal state, violating I3; `tests/control/test_states.py` proves both the gap and the closure. |
| A-6 | 1 | RETIRED is reachable from CANDIDATE, MATURED and QUARANTINED, not only ACTIVE. | An antibody that fails maturation or refinement must be closable. |
| A-7 | 1 | E.2, E.5 and E.6 schemas require real 64-hex hashes, `sha256:` digests and passphrase nonces; the spec's symbolic examples (`h_811`, `9a41…`, `9f2c4e…`) are illustrative and fail them by design. E.4 prose placeholders are normalised to identifiers in `schemas/E4_repair_receipt.template.md`. | Schemas are the audit contract and must reject placeholders. |
| A-8 | 1 | Motif kind `function` accepts snapshot kinds `function`, `method`, `lambda`, `closure` until spike A9 pins the real snapshot kinds. | The kind vocabulary of compact-ndjson snapshots is not yet observed. |
| A-9 | 1 | Helper modules beyond the Stage 1 file list: `kintsugi/proof/passphrase.py`, `kintsugi/proof/schemas.py`, `kintsugi/gates/protected_paths.py`, `typings/pyrage` stubs, `tests/fixtures/spec/` (verbatim Appendix E examples). | Implementation support for A-3, A-7, the protected-path policy, and pyright on the compiled `pyrage` module. |
| A-10 | 2 | entire-graph v0.4.0 has no `context`, `callers` or `callees` subcommands. `kintsugi/memory/graph.py` provides them as `search --format agent`, `neighbors --direction in` and `neighbors --direction out`. | Verified: the plugin answers "unknown command". |
| A-11 | 2 | `entire doctor` is an interactive fixer and is never invoked by Omega. Health checks use `entire graph doctor --json` and `entire status`. | Verified from its help text: it "offers to fix" session state. |
| A-12 | 2 | Gateway hardening: npm `.cmd` shims are resolved to their `.exe` and batch files are refused; stdin is written as exact bytes; long prompts go in on stdin from a file. | Real Windows bugs: a shim would pass prompt text to `cmd.exe` (`&`, `|`, `>`), text-mode stdin rewrote `\n` as `\r\n` (corrupting `git commit -F -` and `git mktree`), and command lines are capped near 32k characters. |
| A-13 | 2 | entire-graph snapshot schema 1.1 is pinned with golden files. The graph also credits a closure's calls and constructions to its enclosing function, so the motif binds the retried scope `F` through `CONTAINS`, never through the enclosing function. | Spike A9; the reconciled v1.0/v1.1 motifs are validated on real snapshots. |
| A-14 | 2 | A checkpoint ref's `i/content_hash.txt` is `sha256(i/full.jsonl)`; offline audits read checkpoints with plain git and verify that hash. | Verified on real checkpoints. |
| A-15 | 2 | `.entire/settings.json` carries `redaction.custom_redactions` (a name → RE2 map; Entire rejects list forms): Anthropic, neatlogs, bearer, GitHub, AWS, age and private-key rules, plus the permanent `kintsugi_redaction_probe` positive control. The same rules drive a pre-push scan of checkpoint trees. | Spike N1: rules load live (`inline_patterns: 8`), the probe is redacted, passphrase tokens survive, a base64 decoy is blanked by Entire's built-in scanners. |
| A-16 | 2 | `orchestrator_commit` refuses staged changes that the session's Write/Edit tool calls do not explain (`session_paths`). | A real checkpoint did not record a file written by a shell command; C4's `files_touched` clause caught it. Refusing before commit keeps C4 green by construction. |
| A-17 | 2 | `Kintsugi-Prompt` is `sha256` of the exact UTF-8 bytes of the session's first prompt, which is delivered on stdin; the attestation's `prompt_template_sha` field carries the same digest. | Makes C5 a byte-exact check. |
| A-18 | 2 | Headless sessions run with `--setting-sources project,local`, `--strict-mcp-config`, an orchestrator-chosen `--session-id` and `--max-budget-usd`. | A headless session otherwise loads the user's plugins and skills; a pre-chosen session ID lets L2 adopt an attempt by ID. |
| A-19 | 2 | Checkpoint-ref sync is judged by the remote's final state, never force-pushes, and scans for secrets before pushing. | Entire's pre-push hook races explicit ref pushes ("cannot lock ref … expected"); a diverged ref may be evidence. |
| A-20 | 2 | The model tier of spikes A1/N2, N1, N5 and A14 waits on a logged-in headless CLI (`claude auth login`) or the Stage 3 `ANTHROPIC_API_KEY`. The tests exist and skip with that reason. | The desktop app's auth is host-scoped; it is not borrowed for unattended processes. |
| A-21 | 2 | Commit trailers are parsed across consecutive trailing trailer-only paragraphs. | A hook may append `Entire-Checkpoint:` in its own paragraph after the `Kintsugi-*` block. |
| A-22 | 3 | Architect ruling: the model tier of spikes A1/N2, N1 (headless), N5 (commit linkage) and A14 (mid-tool kill) is carried into Stage 3 and runs once a model provider is wired. Result: the headless CLI was logged in (`claude auth login`, claude.ai) at the start of Stage 3 and all four passed on the first run (`docs/SPIKES.md`). | Stage 2 closed without a logged-in headless CLI (A-20). |
| A-23 | 3 | Architect decision, host model provider: **offline first, key later**. Hosts call Claude through `kintsugi/sandbox/llm_proxy.py` only; until `ANTHROPIC_API_KEY` is added to `.env` the proxy's upstream is a deterministic, clearly labelled offline stand-in per host (`hosts/<h>/evals/offline_model.py`), and baselines recorded with it are labelled `model=offline`. The live Anthropic upstream is built and tested against a fake server; live baselines are re-run when the key arrives. Bedrock is not used yet: `GetFoundationModelAvailability` reports `NOT_AUTHORIZED` for Claude models in account 841535407298 (AWS case 179171381300975). Hard daily cap across all proxy accounts: **$20/day** (ruling D7). | No Anthropic key in `.env`; Bedrock access pending. |
| A-24 | 3 | The disclosed background chaos profile (`kintsugi/sandbox/profiles/background.yaml`) adds HTTP 429 on 4% of Scout's `fetch_document` calls to the spec's `fx_rate` 429 on 8% plus mild jitter. Chaos decisions are `HMAC-SHA256(seed, rule ‖ service ‖ task ‖ request ‖ occurrence)` draws, so faults are identical per seed under any thread interleaving; an injected status never reaches a side-effecting upstream. Helper module `kintsugi/sandbox/httpserve.py` (shared threaded server for the proxies and mock services). | §6.7's L3 path needs "multi-page questions that hit a 429" in Scout's live traffic; the spec's profile only names `fx_rate`. |
| A-25 | 3 | Sandbox topology (D2): Docker Engine and gVisor `runsc` run natively in WSL2 Ubuntu (Docker Desktop's VM cannot host `runsc`); Windows reaches them through `wsl.exe -d Ubuntu --exec docker` via the subprocess gateway. Sandboxes join an `--internal` network whose only exit is the stdlib egress gateway (`kintsugi/sandbox/egress.py`), pinned in `/etc/hosts` with `--add-host` because Docker's embedded DNS does not answer under `runsc` (spike N8). Credentials stay outside: the runner refuses credential-like env vars unless the value is a `kintsugi-proxy-` placeholder, and the gateway injects the real header per route. | Spike N8; spec §5.10 "no secrets inside sandboxes". |
| A-26 | 3 | Fleet layout (D3): each host is a uv workspace member (`hosts/<id>/pyproject.toml`, package `<id>`) with `host.yaml` (SLO, economics, tool registry with `side_effect`, services, offline model); evals and graders live inside the package (`<id>/evals`, `<id>/graders`, both protected by `**/evals/**`, `**/graders/**`) so they import cleanly; shared runtime `hosts/hostkit` (task context, neatlogs init and the `grader.verdict` span, external calls via the chaos proxy, model client via the LLM proxy, eval runner). Holdout custody (`kintsugi/gates/holdout.py`): generated from a secret seed that exists only inside the age payload, a passphrase canary printed in every holdout task (I5 leak marker; only its SHA-256 is public), recipient in `.kintsugi/holdout.pub`, hashes in `.kintsugi/holdout.sha256`. The Stage 3 holdouts are sealed to a **rehearsal** identity kept outside the repo; the T0 checklist (Stage 13) re-seals them to an Architect-held identity, like the attestation key (A-4). Health cards are written to `evidence/baselines/<host>.json`. | Keeps hosts runnable as their own repos while sharing one lockfile; makes the holdout non-regenerable from public code. |
| A-27 | 3 | (1) The hosts' own test suites deliberately do not exercise a 429 in the middle of a Ledger batch or a Scout document: the spec needs S-17 to pass the host's normal CI (§6.2 step 3) and a mis-applied vaccination to pass G3 and fail only in the canary (§6.7). Pre-sabotage correctness is verified instead by the baselines (zero duplicates under the background 429s) and the fleet motif contract on the live graph. (2) Triage's offline stand-in declares a capability gap: the Haiku stand-in reads English keywords only (cognates still match), larger tiers also read a multilingual lexicon. It is a simulation parameter for offline runs; in live mode the L5 story (§6.8) depends on how the real Haiku handles mixed-language titles, which the live baseline will measure. (3) Scout's documents end with a page that states no fact, so a 429 after the fact page is common enough for the L3 canary signal. | Keeps the cascade realistic while recording what the offline mode simulates. |
| A-28 | 4 | Each task is one trace: `hostkit.evalrun` opens a `task:<id>` WORKFLOW root around the agent and its grader, so `grader.verdict` shares the task's trace id. | Read back through the Public API, every Stage 3 verdict was a one-span trace of its own (emitted after the agent's root closed); an R-007 hit could not reach the tool spans. |
| A-29 | 4 | Public API credentials: `NEATLOGS_SERVICE_TOKEN` (service account, `nlsa_`) with `NEATLOGS_PROJECT_ID`. Webhook test and delivery replay, detection preview, generate and backfills accept only an OAuth user token (`NEATLOGS_OAUTH_TOKEN`, not configured); the client raises `CredentialUnavailable` before sending. Preview, generate, backfills and custom classifiers are also behind the disabled `tier-c-compute` gate. Fallbacks: the poller replaces delivery replay (L2); Stage 7 measures receptor precision on historical traces itself instead of `POST /detections/preview`. | Verified live: the SDK ingest key is refused (401); the pinned contract lists the accepted schemes per operation. |
| A-30 | 4 | The client is pinned to a 50-operation subset of the published OpenAPI 3.1 document (`kintsugi/senses/contract/`, with the full document's SHA-256); request bodies and golden responses validate against it, and a live contract test fails on drift. | Spec "pinned OpenAPI + golden fixtures"; the full document is 7 MB and 285 operations. |
| A-31 | 4 | Receptors as built: R-001 regex (tool spans per host, error text included; paging threshold above the background chaos rate: Ledger/Scout 9, Triage 3 per 10 min); **R-002/R-003/R-004 are Omega-side metric receptors** evaluated by the poller over trace summaries (thresholds per host: tool calls max(10, 2 × healthy max) = 54/54/10, c_max = 10 × budgeted cost per task, l_max = p95 SLO or 30 s); R-005 uses the built-in `refusals` classifier as a stand-in (off-task and evidence checks move to the Stage 6 independent grader); R-006 PII on the host's answer span, entities per host (v2: Ledger drops `PHONE_NUMBER`, which matched 137 healthy journal-line answers); R-007 regex `^"?FAIL:` on `grader.verdict`, scoped per host by the task-id prefix in the span input. Detections are project-wide, so per-host scoping is by span name or input pattern. | neatlogs conditional detections accepted every documented field name but never fired; the spec's fixed `tool_call_count > 10` would page on healthy Ledger and Scout tasks (up to 27 tool calls); span outputs are JSON-encoded (`"FAIL:…"`). |
| A-32 | 4 | The shared dedupe key is the natural hit key `(detection, trace, span)`; the API's hit id is stored alongside. `seen_hits` is keyed `hit_key` (the §5.4 `hit_id` column's role). | A webhook payload carries the detection, trace and span ids but not the hit id the poller sees. |
| A-33 | 4 | The poller keeps a per-detection **watermark** (newest `detectedAt` swept), not a cursor: each sweep starts a fresh cursor chain at the newest hit and pages back to the watermark minus a 10-minute overlap; an expired cursor restarts the sweep. | Hits are listed newest first and cursors expire 15 minutes after a chain starts, so a cursor cannot be kept across 60-second cycles. |
| A-34 | 4 | Ingress security: `X-Neatlogs-Signature` (`sha256=` HMAC-SHA256 of the raw body) is required, plus the 256-bit secret path and an optional source allowlist; event ids are deduplicated for 24 h and hits by key. | Spike A6: the signed material has no timestamp, neatlogs does not retry a failed delivery, and replay is OAuth-only. |
| A-35 | 4 | Tag schema: `split` also accepts the fleet's `holdout` and `live-<n>`; `phase:rehearsal` labels unsealed practice runs; tag-write idempotency keys are base64url SHA-256 of (trace, revision, tags). | The eval runner already writes those splits; rehearsals must never count as baselines; the API refuses UUIDv5 keys (`INVALID_IDEMPOTENCY_KEY`). |
| A-36 | 4 | The detection rehearsal uses a disclosed environment fault (`kintsugi/sandbox/profiles/rehearsal-fx-outage.yaml`: the FX API answers 503) so Ledger emits real `FAIL:` verdicts with no host code change. | Stage 4 must show R-007 firing on Ledger failures without committing any sabotage mechanism. |
| A-37 | 4 | Entire 0.11.4, several commits in one turn: after a commit, files edited earlier in the turn but left uncommitted are "carried forward", and while that set is non-empty the next commits are attributed **only** against it. Here `pyproject.toml` was edited before the first commit and committed in the fourth, so commit 3 (`01M4NRV5K0828Z1TWGGGYGREM4`) has `files_touched: null` and commit 4 (`01M4NVAAQKHEQ58DCBE9HK0S46`) lists only `pyproject.toml`; trailers, refs and transcripts are intact, and every other Stage 4 commit is fully covered. Rule: never leave edited files uncommitted between the commits of one turn. | Observed on this stage's commits 3 and 4; the carried-forward set is logged as `post-commit: carried forward remaining files`. |
| A-38 | 4 | Receptor alert rules are `per_event` webhook hints; the "≥ N hits in 10 minutes per host" paging rule (§4.1.4) is applied by the Sentinel from each receptor's `threshold_count`/`window_minutes`, carried in the manifest. **Open platform issue:** after the first per-event delivery (spike A6, 14:57 UTC) no alert rule in the project fired again in about an hour, neither the 12 threshold rules (R-007 had 5 hits within a second, three times; R-001 had 193) nor the 12 per-event rules, nor the unchanged spike rule on fresh hits; alert history holds one fire. Detections kept recording hits within seconds. The project is on the free tier; the Architect is asked to check alerting in the neatlogs UI or with support. Omega does not depend on it: the poller is the source of truth (§4.1.7) and recovered every hit. | neatlogs threshold rules never fired in three rehearsals; webhooks are at-least-once hints by design. |
| A-39 | 4 | Known ingestion loss: about 3–4% of task traces never become visible (Stage 3 baselines: Ledger 695 of ~720 agent traces, Scout 173/180, Triage 291/300; each 6-task rehearsal lost exactly one, still absent after 20+ minutes). A failure whose trace is lost raises no hit, so I7 can only cover what neatlogs reports; the live verdict rows (`.state/runtime/*.jsonl`, eval JSONL) remain Omega's own record. Investigating the SDK export path (flush/shutdown under concurrent workers) is carried into Stage 5 (Watchdog health checks) and Stage 13. | Risk R12; measured, not yet explained. |
| A-40 | 5 | Temporal runtime (D6): temporalio 1.34.0; the SDK fetches its single binaries on first use, the time-skipping test server (unit tests and CI) and the Temporal CLI dev server (live drills, contract tests). Unit tests drive coroutines with `asyncio.run`, so no async test plugin is added. | Both binaries are official single-binary servers, which is what D6 rules; CI ran the time-skipping tests green on Linux. |
| A-41 | 5 | Activity contracts: the §5.3.2 table is encoded row for row, and the loop's other steps (§5.13: `route_hit`, `record_transition`, `tag_traces`, `match_antibody`, `fixes_prior`, `hypothesize`, `rewind`, `rebase_and_rerun`, `budget_exhausted`, `park`, `escalate`, `quarantine_related`, `backfill_receptor`, `fixes_resolve`, `canary_sample`, `apply_antibody`, `motif_scan`, `onboard_step`, `seal_sabotage`, `deliver_sabotage`, `anchor_ledger`) get explicit contracts. Each runs on one of three pools (`kintsugi-crew`, `kintsugi-surgeon`, `kintsugi-senses`). Idempotency keys of per-pass steps carry a pass number (0, +1 per canary re-entry); transitions carry their sequence number; hypothesis ids are unique within an incident across passes. Results are cached by key in a SQLite step ledger shared by every worker. | A REPAIRING → REPAIRING retry and a canary re-entry would otherwise collide with earlier keys and replay stale results. |
| A-42 | 5 | Incident semantics the spec leaves open: the **secondary response** passes the antibody's recipe to the Coordinator as its prior (the incident still runs G0 and the gates, so the transition model is unchanged); a **canary re-entry** stays in REPAIRING (`CANARY → REPAIRING`) and re-runs triage, reproduce, attribute and plan as work inside REPAIRING, instead of §5.13's recursive `self.run`; **G7 failing** on the winner's PR ends ESCALATED through REPAIRING's exit (nothing is SHIPPED); a **Canary** that runs out of batches without crossing a boundary accepts (no evidence of a regression at the planned power); a Coordinator naming a winner that did not pass G0–G6 fails the workflow (I1). | States.py's model (A-5) is the authority; every path must fold to a legal log. |
| A-43 | 5 | FleetSupervisor: the Sentinel's routing is the `route_hit` activity; each Incident signals `incident_closed` back, so the per-host limit survives `continue_as_new`; up to 5,000 recent hit keys are carried across runs (the senses store dedupes for good); `operator_resume` complements `operator_pause`. | `continue_as_new` drops child handles; signals do not. |
| A-44 | 5 | Reduced mode: `seen_hits` stays in the senses store (A-32); the `ledger` table uses `kintsugi.proof.ledger`'s record format so C1 verifies it; the single writer runs §5.4 steps 2a, 2b and 3 at start (`recover`) and the restarted **executor** re-leases its step through the idempotency key (2c); the local socket is TCP on 127.0.0.1 with a 256-bit token. | One writer must not run crew work; AF_UNIX sockets are not dependable on Windows. |
| A-45 | 5 | Surgeon adoption: a `surgeon_attempts` registry records each nonce before its session starts and stands in for the ledger's attestations until Stage 8; adoption also kills any orphan still holding the worktree lock; a commit whose nonce was never issued is never adopted. | §6.6 adopts "a commit whose Kintsugi-Nonce matches an attestation in the ledger"; the ledger arrives in Stage 8. |
| A-46 | 5 | Watchdog: every chaos call must name its sabotage id and is appended to `chaos.jsonl` (sealing itself is Stage 8); the ingress outage uses a new `/admin/availability` endpoint with its own 256-bit admin token; the dead-man switch is a heartbeat file plus an external `deadman` check (its minimal Lambda deployment is Stage 13); the neatlogs `ingestion_health` rule waits on A-38. | §5.5; the chaos API is the Saboteur's only lever and must leave a record. |
| A-47 | 6 | Sentinel dispositions: a hit below its receptor's paging threshold is recorded as `held` against the watch record `WATCH-<receptor>-<host>` (a deduplication record for I7 and C9); when the threshold is reached within the window, the held hits are attached to the incident it opens. Stage 4 stores migrate in place. | With only `opened`/`attached`, background-chaos hits that never page would stay pending forever and fail I7. |
| A-48 | 6 | G3 runs `entire graph verify` with `pytest -rA` (per-test results). The plugin always exits 0, so its verdict is parsed (`NEWLY FAILING (n)`). Where Entire is not installed (G7 in Actions) the same delta comes from two `pytest -rA` runs. | Verified: with `-q` the plugin cannot parse test ids and degrades to an exit-code verdict that cannot tell a pre-existing failure from a flip. |
| A-49 | 6 | L4: a winner chosen while main moved is re-verified **merged onto the new main** in a scratch worktree (G1 and G2 always, G3 limited to the tests inside the impact radius, G4 re-run only when the `entire graph diff`/`impact` radii intersect, otherwise reused with the reason recorded); the attested commit is never rebased. The Incident workflow does this behind `workflow.patched("l4-reverify-winner")`; a failed re-check or a merge conflict counts the round as failed. | Rebasing an attested commit changes its parent and tree, which voids its signature (C3) and checkpoint linkage (C4); G7 tests the PR's merge ref anyway. |
| A-50 | 6 | Repair targets declare their hosts in `.kintsugi/target.yaml` (protected), including `live_services`: services that hold the host's own state (the practice ledger's books API) are never recorded in or replayed from a cassette, so a replay recomputes that state against the code under test. | Replaying a stateful service's recorded listing served the duplicates the failing code had left, so no fix could pass G2 (found by the gate tests). |
| A-51 | 6 | Omega's PRs merge with a **merge commit**; rulesets on the branches Omega repairs require a pull request and the G7 check and forbid force-pushes and deletion, but not linear history. Ruleset conditions list branches explicitly, each added right after its creation. | Spike N9: GitHub's rebase and squash merges create new commits, which would orphan the attestation signed over the original tree and parent; a required status check also blocks the push that *creates* a matching branch. |
| A-52 | 6 | A delivery prompt (rehearsal now, the Saboteur's S-17 in Stage 8) must not promise behaviour its patch contradicts. Claude Haiku 5.5 read the practice patch, saw it moves the posting session into the retried closure, and declined to apply it under the framing "keep behavior identical"; with the spec's own §6.2 framing ("refactor for readability … apply this patch and run the existing tests") it applied it. | Observed in the first Stage 6 rehearsal attempt; Stage 8 must plan for a delivery model that reviews the patch. |
| A-53 | 6 | G7's fresh-seed G2: the incident's error class (`Kintsugi-Symptom`, φ's error class, named in the PR body) must not occur on the head at any of three fresh seeds under the target's stress profile, and the head may not fail more often than the base for any other class; without a symptom the strict 3/3 rule applies. The local G2 (the exact G0 cassette) stays 3/3. | Under the 30% 429 stress profile a correct fix can exhaust its 5-attempt retry budget (`RateLimited`); the first rehearsal's G7 rejected a correct fix that way (PR #2, closed with this explanation). |
| A-54 | 6 | Crew models via the headless CLI 2.1.269: `claude-opus-5-5` is refused ("2.1.280 or newer is required"), so the Opus roles (Pathologist, Archivist, Coordinator, Auditor G5) run on `claude-opus-5` through the per-role override, recorded per call in `calls.jsonl`, until the Architect updates the CLI; the Sentinel (`claude-haiku-5-5`) and the Surgeons (`claude-sonnet-5-5`) run as specified. | Verified live; updating the CLI is a download left to the Architect. |
| A-55 | 6 | Decision rule details: a G5 `concern` (for example a wholesale revert) passes the gate but is eligible only as a last-round fallback; Pareto dominance ignores differences within measurement noise (2 points of pass rate, 2% cost, 5% p95), so equivalent fixes go to the pre-registered experiment (stress profile, the same tasks and seeds per candidate; pass rate, then mean cost, then diff size), whose protocol hash is checked before it runs. The Auditor runs G4's base and candidate back to back per seed in every audit. | §5.7 leaves "rank down" and "two Pareto-optimal candidates" open; wall-clock p95 noise otherwise picks a winner between identical fixes, and base runs reused across audits were measured under different load. |
| A-56 | 6 | Information flow is enforced by the incident's case file: each role reads only its §5.7 columns (refused reads are logged); the governor reads the incident opening for its VaR; deterministic tools (bisect, gate runners) may load the G0 cassette and repro record to execute them; a Surgeon's self-report and session record are readable by no other role. A refused commit (protected path, budget, no edits) is the orchestrator's `refusal` and fails the hypothesis, not the incident (I3); no Surgeon session starts once the budget is spent (G6). | Makes "the Auditor never sees Surgeon output" a checked property and keeps I3 when one Surgeon misbehaves. |
| A-57 | 6 | Rehearsal target (Architect decision 2): a disclosed, disposable **Ledger practice** host (`tests/fixtures/rehearsal/ledger_practice`, on the approved `ledger_before` shape) whose practice regression is a committed fixture; each run copies it outside every repository and ships through `rehearsal/<run>/*` branches of this repository, never `main`. The real Ledger host's S-17 patch stays unwritten. Ledger-shaped steps append to a file-backed hash-chained ledger until Stage 8; the Archivist falls back to git-only localisation where entire-graph is missing (CI). | Keeps sabotage secrecy while exercising the whole loop, GitHub included. |
| A-58 | 6 | Stage 6 commit `fd102e6` (checkpoint `01M4PEC6NRXTW5EJZ99QDZ06NR`) lists 6 of its 7 files: the session hit its usage limit and the turn ended with those edits uncommitted, so under A-37 the next commit was attributed only to the carried-forward files and `tests/control/test_workflows.py` (a test-only change) is missing from `files_touched`. Trailer, ref and transcript are intact. | A-37's rule cannot hold across an involuntary turn end. |
| A-59 | 7 | Supersedes A-54: the Architect updated the headless Claude Code CLI to 2.1.296, and a headless probe ran `claude-opus-5-5` natively. The Opus roles run on `claude-opus-5-5` as §5.7 specifies; no `--model <role>=claude-opus-5` override is passed any more. | Verified 2026-10-12 (`claude --version` = 2.1.296). |
| A-60 | 7 | The fleet's repair targets are declared in the monorepo's `.kintsugi/target.yaml` (ledger, scout, triage; Triage's mock GitHub is a live service, A-50), with a disclosed fleet stress profile (`kintsugi/sandbox/profiles/stress.yaml`: 30% 429s on `fx` and `docs`) for disputes, G7's fresh-seed G2 and the antibody's induced trigger. Before any real Sentinel run the senses store's rehearsal history is archived (`SenseStore.archive`: an online backup, then the live store drops its claimed hits and queue but keeps watermarks and webhook event ids). | The crew can only repair or vaccinate hosts it has a target for; stand-in dispositions (`INC-REH-…`) must not reach a real Sentinel or I7. |
| A-61 | 7 | Until Stage 10 onboards real Repair Clinic and OSS hosts, a disclosed **CRM practice** host (`tests/fixtures/rehearsal/crm_practice`) stands in for Clinic-A (§6.5 step 13): it syncs contacts to a CRM (side-effecting) through a rate-limited geo API, carries the state-reset-on-retry defect latently by design, and is treated as a foreign repository (pull requests only, never merged). The background profile appends a 2% 429 rule for its `geo` service (appended last, so the fleet's draws are unchanged), which leaves silent past `DUPLICATE_CONTACT` occurrences for a backfill to find (O6). Graph goldens `crm_practice` and `ledger_practice_before` (AB-1's origin before its fix) join spike A9's; `predicates.host_graph` adds the method calls entire-graph leaves unresolved when a receiver's type is only implied (`current = session or S(batch)`; `current.push(…)`), marked `inferred_calls`. | Maturation needs a second vulnerable host, the backfill needs history, and contrastive learning needs three positives; without the inferred calls the side-effect precondition (§6.7 step 19) cannot see a tool reached through the accumulator. |
| A-62 | 7 | The Immunologist's row of the §5.7 matrix: it reads failing traces, summaries, source, intent, cards, diffs, verdicts, budget and priors (never holdout tasks or Surgeon reasoning) and writes a new `antibodies` column plus, for a vaccination only, the case artifacts a secondary response has no Pathologist, Archivist or Coordinator pass for: the trigger cassette and G0 record, the card (its approach is the recipe rendered on the host's own binding), the plan. A recipe names its bindings only through `{S} {F} {R} {Enc} {intent}` slots; a model draft without them falls back to the template. | A vaccination must pass the same blind Auditor (G0–G6) as a primary response, so its case file needs the same artifacts. |
| A-63 | 7 | The induced trigger (§6.5 step 14) is concrete: HTTP 429 at the stress rate (30%) on the external services the motif's retried scope reaches (registry tools with `external:`), over 20 train tasks at seeds 1–2; it "produces a failure" when a task fails that passes at the same seed without the trigger, and those runs, re-recorded and replayed 3/3, are the vaccination's G0 cassette. Scout fails it by exhausting its retries (each attempt restarts at page one: `FAIL:RATE_LIMITED`), our analogue of the spec's `FAIL:TIMEOUT`, so the rule admits Scout exactly as §6.5 narrates. | The spec names the trigger but not its dose or its comparison. |
| A-64 | 7 | Receptors are `grader.verdict` regex Detections on the pathogen's verdict family (`^"?FAIL:DUPLICATE_`). Their precision (`/detections/preview` is OAuth-only, A-29) and the backfill (`/detections/{id}/backfills` needs the detection created through the API) are measured by Omega over recorded runs: a flagged run is a true occurrence when it has the pathogen's error class on a host whose code carries the motif; a backfill hit is "silent" when no incident claimed it, priced at the host's value per failed task. The canary's H0 is the Wilson 95% upper bound of the host's recorded train failure rate (the fleet passes 100%, so the observed rate cannot be H0). | A-29; and the SPRT needs 0 < p0 < p1 < 1. |
| A-65 | 7 | Trace-to-test (§6.7 step 18) commits **seed-addressed regression cases** (task, seed, profile, the verdict when found) under `<package>/evals/regressions/`, not a recorded cassette: the chaos proxy is a pure function of the seed (A-24), so a case replays exactly, and it fits a notary's patch. The commit is made by a notary session under the new `eval_augment` protected-path scope (data only: no code, tests, graders or holdout), after the Auditor's check that each case failed on the vaccinated merge and passes on the revert. Reverts, eval-augment and drill deliveries are all notary sessions (plan §2a L3), each an attested, checkpointed commit; a revert whose tree differs from `git revert -m 1` is refused. | A cassette is too large for a notary's Edit/Write patch, and C4 needs a checkpoint for every commit. |
| A-66 | 7 | **L3 is exercised as a disclosed drill.** In our fleet autoimmunity cannot arise naturally: Omega's gates refuse the recipe's literal application to Scout (its replayed trigger still exhausts the retries, so G2 fails), and a host-adapted application (a resume cursor) is a correct fix the canary would accept. The Stage 7 rehearsal therefore *injects* the literal vaccination on an unprotected rehearsal branch (delivered by a notary session, the Auditor's verdict recorded as it is) and lets everything after the merge run as Omega's own response. A minor version (motif or precondition changed, recipe unchanged) carries its maturation's gate evidence over: re-maturation re-binds the matured host instead of re-running the recipe. | S5 allows "a disclosed test"; the drill keeps the response real while disclosing the one injected step. |
| A-67 | 8 | **Ledger, anchors and the window record.** The ledger lives on the unprotected, append-only `ledger` branch as one file `ledger.jsonl` (§7.3 format, h_0 = SHA-256("kintsugi-omega-genesis" ‖ T0 ‖ raw T0 Ed25519 key)); each append is one plumbing commit (`commit-tree` on the previous tip, compare-and-swap `update-ref`), pushed fast-forward only, never touching a working tree; seq 1 is a `genesis` entry. Ledger-branch commits carry no session and are outside C2–C5: the chain plus anchors are their proof. Each hour an annotated tag `anchor-YYYYMMDD-HH` (UTC) carries the canonical JSON `{h, seq, sig, tag}`, `sig` = Ed25519 over SHA-256(JCS({h, seq, tag})) with the T0 key; Omega writes the tag and the post text, the Architect posts it. `.kintsugi/window.json`, committed at T0, fixes W for the audit: `t0`, `t1` (after W), `repo`, `branch`, `base` (main's SHA at T0), `attest_pubkey`, `holdout_manifest` (sha256 of `.kintsugi/holdout.sha256` at T0), `rulesets`, `g7_check`, `ledger_ref`. Audit semantics: C2 counts a merge commit only when its tree equals `git merge-tree` of its two parents; C1 also checks every published bundle's digest against its ledger entry; C6 ordering is the ledger's `sabotage/sealed` entry before `sabotage/delivered`, and no delivery before `not_before`; a check without its inputs is SKIP (exit 2), never PASS. | D5 makes a wrong entry format permanent, so it is fixed before the first real entry; GitHub's own merge commits carry no attestation and must be justified by content. |
| A-68 | 8 | **The Saboteur.** Specs come from the 17 Appendix B classes; the deterministic rule set (catalog, fleet-only target, patch inside `hosts/<host>/` and outside the `surgeon` scope, no destructive or secret-touching additions, honeytoken absent from the patch, framing free of the mechanism, id, honeytoken and sabotage words) runs before a rubric that must reach 0.7. The rubric is a port; the default is the deterministic fallback (framing, mechanism, patch size, ground-truth files and symbols touched), so tests and dry runs spend no model quota; the Haiku rubric is not wired. Spec+salt and the patch are age-encrypted into a store outside every repository, with a public copy of the record; re-sealing an id returns the existing record. Sealing happens before T0 (Architect decision): the commitments go into the T0 commit and the first ledger entries, so no `saboteur/seal` commit is needed inside W; inside W a seal is published by a notary (`seal-commitment`, scopes `surgeon` + `eval_augment`). Delivery is a notary session (`upstream-change`, incident `UPSTREAM-<host>`, never the sabotage id) whose purpose is the spec's own framing plus "Apply this patch and run the existing tests" (A-52), leak-checked over every field; `deliver(request, host)` is a runtime port (worktree, notary, upstream-lane PR) bound at Stage 13. Reveals are `sabotage-log/<id>.reveal.json`. Only patch classes are delivered; chaos-proxy classes are not wired. | Keeps S-17 sealed and unwritten in clear (A-57) while the whole seal, deliver and audit path is exercised on a disclosed practice spec. |
| A-69 | 13a | **W runtime wiring as built (Master Edition).** (1) `kintsugi.crew.runtime:crew` is the W crew factory, configured by a runtime file named by `KINTSUGI_RUNTIME` (paths only, outside the repository); it refuses a missing or closed window record, a ledger branch without its genesis entry, any key or age identity inside a git worktree, and a T0 key that is not the window's `attest_pubkey`. The Watchdog passes `KINTSUGI_RUNTIME` and the neatlogs Public API credentials to its pools explicitly (the gateway's allowlist would drop them). After each anchor the ledger branch and tags are pushed fast-forward. (2) The upstream lane (`kintsugi/crew/upstream.py`) lands a delivery or a commitment as a notary commit on `upstream/<host>-<UTC minute>` or `upstream/seal-<UTC minute>`, merged with a merge commit only when the repository's normal CI on the PR is green. (3) `scripts/w-runtime.sh` is the WSL launcher; it installs nothing (the Temporal CLI in WSL is the Architect's install). (4) Step 7 runs as a **deterministic W wiring rehearsal** (`kintsugi/crew/w_rehearsal.py`, evidence `evidence/rehearsals/w-20261012-193427.json`): practice host, rehearsal key, genesis, a sealed practice spec delivered through the real upstream lane with two disclosed stand-ins (an offline notary session and local pull requests), anchors, reveal; audit C1 and C6 PASS. C2–C5, C7–C10, C12, C13 need real sessions, GitHub, neatlogs or a sealed holdout and move to the Stage 13b dress rehearsal. (5) Step 4 (S-17/18/19) is not done by a build session: every Claude Code session in this repository is checkpointed by Entire and its transcript is pushed with the code, so composing the specs here would put the mechanisms in a public transcript (A-57); sealing also needs the T0-held age recipient, which exists only in 13b. The Architect composes and seals them outside any Entire-tracked session. | Architect scope for the overnight run (2026-10-13): offline-first rehearsals; no model quota spent where a deterministic run shows the wiring. |