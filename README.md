# KINTSUGI-Ω: An Immune System for AI Agents

> Repairs that show their seams in gold.

**Master Edition.** This repository continues KINTSUGI-Ω beyond the hackathon (Stages 9–13). The
#neatHack submission is frozen at
[suryaprasanthcse/kintsugi-omega@13a744b](https://github.com/suryaprasanthcse/kintsugi-omega/tree/13a744b)
and is never modified. This edition has since built the W runtime wiring (Stage 13a), the
economic cortex (Stage 9), self-repair (Stage 12), the offline OSS-1 onboarding rehearsal
(Stage 10) and the Arena board and service (Stage 11), all verified offline; see
[Master Edition progress](#master-edition-progress-2026-10-13). The plan is in
[MASTER_HANDOFF.md](MASTER_HANDOFF.md).

KINTSUGI-Ω is our #neatHack 2026 entry: an autonomous reliability system for a fleet of AI
agents, modelled on the adaptive immune system. It senses failures with
[neatlogs](https://neatlogs.com), remembers and proves every change with
[Entire](https://entire.io), and prices every repair with [cfo.ai](https://cfo.ai).

## The problem

AI agents fail in production quietly. A refactor ships, the tests stay green, and a retry starts
posting duplicate ledger lines. Today's coding agents:

- **ship unverified fixes**: a patch that passes unit tests can still regress on the traffic
  that broke it;
- **pay full price for every repeat**: the same bug on the next repository costs a whole new LLM
  investigation, so repair cost grows linearly with the fleet;
- **ask to be trusted**: their logs and "fixed it" claims are self-reported.

## How it works

```
 neatlogs traces ──► SENSES ──► CREW ─────────────────────► GATES G0–G7 ──► SHIP (PR, CI)
 (grader.verdict)    receptors   Sentinel → Pathologist      reproduce · unit · replay ·      │
                     webhook +   (Entire code graph,         paired holdout statistics ·      ▼
                     poller      blast radius) → Surgeons    intent · budget · independent   CANARY (SPRT)
                                 (competing, sandboxed) →    CI                               │ reject → auto-revert
                                 blind Auditor                                                ▼
 MEMORY: Immunologist turns a solved incident into an antibody (motif on the code graph +
 receptor + recipe) → matures it on another host → vaccinates the fleet; harm → quarantine,
 contrastive refinement.
 PROOF: every commit signed (Ed25519) and linked to its Entire checkpoint; hash-chained ledger
 with signed hourly anchors; sealed sabotage; `kintsugi audit` C1–C14 re-checks all of it.
```

- **Senses**: neatlogs receptors watch the `grader.verdict` span of every agent run; a webhook and
  a reconciling poller open incidents and dedupe hits.
- **Crew**: headless Claude Code roles. A blind Auditor runs deterministic gates G0–G7. Its G4
  pairs the candidate against the baseline on an age-encrypted holdout that the crew never sees.
- **Provenance**: each crew commit carries signed `Kintsugi-*` trailers and an `Entire-Checkpoint`
  trailer; an orchestrator-decided change (revert, regression cases) runs inside a templated
  **notary** session, so every commit has a session behind it.
- **Economics**: every incident and vaccination is costed; the governor keeps spend under budget.

## Results (rehearsals on disposable hosts)

From the Stage 6 and Stage 7 rehearsals (`evidence/rehearsals/l3-20261012-074523.json`,
[`antibodies/AB-1`](antibodies/AB-1), [docs/VERIFICATION.md](docs/VERIFICATION.md)):

| What | Result |
|---|---|
| Primary response (Stage 6) | `INC-R235459-0001`, duplicate ledger postings on the Ledger practice host, resolved by the crew through gates G0–G7 ([PR #5](https://github.com/suryaprasanthcse/kintsugi-omega/pull/5), G7 green in Actions, canary accepted): **$0.665** |
| Antibody AB-1@1.0 | synthesised from that incident; the motif matches the code before the fix and not after |
| Maturation on a second host | CRM practice host, gates **G0–G6 pass** (G4: b=6, c=0, p=0.0156): [PR #7](https://github.com/suryaprasanthcse/kintsugi-omega/pull/7) (PR-only, a foreign host) |
| Receptor | precision **1.00 over 335** recorded runs → ACTIVE |
| Backfill | **6 silent past failures**, **$12.00** of impact |
| Vaccinations | **$0.089** (CRM) and **$0.104** (Scout) vs $0.665 primary: **~7× cheaper per repeated bug** |
| L3 autoimmunity (**disclosed drill**) | the literal recipe was forced onto Scout past a failed Auditor verdict ([PR #8](https://github.com/suryaprasanthcse/kintsugi-omega/pull/8)); the canary SPRT rejected it at n=38; Omega reverted through an attested notary session ([PR #9](https://github.com/suryaprasanthcse/kintsugi-omega/pull/9), reverse canary accepted at n=70), quarantined AB-1, added 6 seed-addressed regression cases ([PR #10](https://github.com/suryaprasanthcse/kintsugi-omega/pull/10)) |
| Contrastive learning | AB-1@1.1 learned the precondition "a side-effecting callee inside the retried scope" (true on the 3 protected hosts, false on Scout) and re-matured to ACTIVE |

## Master Edition progress (2026-10-13)

All of it offline and disclosed: no model quota spent on rehearsals, no foreign repository
touched, no `.env` in this clone. Evidence and details: [docs/VERIFICATION.md](docs/VERIFICATION.md).

| Stage | Built | Verified by |
|---|---|---|
| 13a W runtime | `kintsugi.crew.runtime:crew` (branch ledger, T0 signer checks, anchors pushed), the upstream lane (`deliver`/`publish` through a notary, a PR and normal CI), `scripts/w-runtime.sh` for WSL | W wiring rehearsal: genesis → sealed practice spec → delivery → anchor → audit **C1 and C6 PASS** (`evidence/rehearsals/w-20261012-193427.json`) |
| 9 Economic cortex (L5) | `business/plan.json` pinned per incident, actuals with sources, the live governor, the L5 decision span, the Treater, `kintsugi econ` (cfo.ai tabs as files, nightly Wright refit) | Triage L5 rehearsal: measured 5.0% failure → **escalate** (no repair at lower power); spec §6.8 inputs → **treat and escalate**, T1 applied, live failures **9/180 → 5/180** (`evidence/rehearsals/l5-20261012-200248.json`); runway **14.4 months** from **disclosed placeholder** cash and fixed costs, labelled as such in `runway.csv` (A-73) |
| 12 Self-repair | the `self_repair` scope enforced at commit for Omega's own repairs (C14), `hosts/omega`: a replay suite of **4 recorded incidents** (of the plan's 20) with a hit@3 grader | INC-SELF-01 rehearsal: root-cause **hit@3 0.25 → 0.75**, C14 PASS, an edit to the audit refused at commit (`evidence/rehearsals/self-20261012-202518.json`) |
| 10 Onboarding (offline) | `kintsugi/onboard` O1–O7: Clinic intake form with required consent, health card, replay mode from exported traces, seed evals, baseline, immune scan, PR-only receipts; `kintsugi onboard rehearse` | OSS-1 rehearsal on the disclosed CRM practice host (OSS-1 itself is the Architect's choice): 24 locally captured traces replayed, the immune scan matches **AB-1@1.1**, receipt **PR-only, no merge**, **0** non-loopback connections, **0** foreign posts (`evidence/rehearsals/oss1-20261013-053800.json`, A-74) |
| 11 Arena (offline) | `kintsugi/arena_api.py` (strict schema, environment classes only, sealed on arrival, 1 request / client / 10 min, daily cap), `kintsugi/arena_service.py` (`Saboteur.seal` + the `SabotageCampaign.request` signal), the static board `arena/` (fleet, incidents, immunity matrix, learning curve, ledger head, "Sabotage it"; no build step, no external requests), `kintsugi arena serve` | a 33-payload injection fuzz corpus is refused; the campaign signal reaches a running `SabotageCampaign` on a Temporal test server; closed before W (503, no limit spent) (A-75) |

## The proof plane: don't trust it, audit it

`kintsugi audit` (spec §7.5) re-checks the autonomy window from public data:

| Check | Verifies |
|---|---|
| C1 | ledger hash chain from h_0, signed hourly `anchor-*` tags, bundle digests |
| C2–C5 | every protected-branch commit attested, signature valid, Entire checkpoint holds the nonce, first prompt hashes to `Kintsugi-Prompt` |
| C6 | sealed sabotage opens only to its commitment, sealed before delivery, honeytoken never leaked |
| C7–C10, C12 | G7 CI runs, trace existence, completeness of detection hits, cost within 1%, rulesets active |
| C11, C13, C14 | antibody lineage replays, holdout manifest unchanged, self-repairs never touch the proof plane |

A check whose inputs are missing reports **SKIP, never PASS** (exit 0 pass · 1 fail · 2 skipped).
A 33-test mutation suite proves each check catches its own tampering: a rewritten ledger caught
by its anchor, a forged signature, a missing nonce, a planted honeytoken and more.

```bash
uv sync --locked
uv run kintsugi audit --all --offline ./published-bundles   # today: C11, C14 PASS; the rest SKIP (no window ran); exit 2
uv run pytest tests/proof/test_audit.py -q                  # 33 tamper tests
uv run kintsugi t0 check                                    # what a real autonomy window still needs
make check                                                  # ruff, pyright, full test suite
```

## Disclosure

| Item | Status |
|---|---|
| Stages 1–8 of the [execution plan](docs/EXECUTION_PLAN.md) (proof cores, Entire layer, fleet, senses, control plane, crew and gates, antibodies, proof plane) | **Built and verified**: 842 tests passed, 30 contract tests skipped (they need local tooling); omega-ci green |
| The autonomous window W | **Not run** (Architect's go/no-go, Oct 12): the runtime wiring for W was not ready in time. The audit therefore SKIPs window checks; they are exercised by the mutation suite instead |
| Stages 9–12 (economic cortex, onboarding/Clinic, Arena, self-repair) | **Descoped** in the #neatHack submission; built offline in this Master Edition (above): **970 tests passed, 30 skipped** in one full run on `f6e1cb8`. Not yet done: cfo.ai's Ari MCP and live C10 (need this edition's own credentials), the real cash and fixed costs (placeholders today, A-73), OSS-1 onboarding on a real repository (the Architect picks it; rehearsed on a practice host, A-74), the Arena's model compiler and chaos-proxy deliveries, 16 more replay incidents, S-17/18/19 (composed and sealed by the Architect outside any Entire-tracked session, A-69) |
| Rehearsal hosts | disposable practice copies (`ledger-practice`, `crm-practice`), shipped through `rehearsal/*` branches; the real Ledger's sabotage mechanism is never written in clear (A-57) |
| L3 | a **disclosed drill**: the wrong fix was injected on purpose; everything after the merge is Omega's own response (A-66) |
| Sealed sabotage | demonstrated on a disclosed practice spec (P-1, audit C6 PASS); the real S-17/18/19 were never sealed because W did not run |
| Baselines | `model=offline` agents (A-23); the crew uses Claude models |
| Open issues | neatlogs alert rules silent (A-38); 3–4% trace loss (A-39); the Haiku realism rubric is not wired (deterministic fallback, A-68) |

Every deviation from the blueprint is an amendment in
[docs/EXECUTION_PLAN.md](docs/EXECUTION_PLAN.md) (A-1 … A-75), and every run, failures included,
is in [docs/VERIFICATION.md](docs/VERIFICATION.md).

## Architect & AI Tooling Disclosure

Surya Prasanth R ([@suryaprasanthcse](https://github.com/suryaprasanthcse)) is the sole human architect and the sole contributor to this repository. Every commit is authored by him; no commit carries a `Co-authored-by:` trailer.

KINTSUGI-Ω is built and operated with AI tooling, disclosed here:

- **Gideon (Google Gemini)**: strategic systems architecture, cryptographic audit design and operational direction, in conversation with the architect. Gideon writes no code and makes no commits.
- **Claude Code**: implementation under the architect's direction, and execution of the headless agent crew (Surgeons, Auditor, Immunologist, notary sessions).
- **neatlogs** ([@neatlogs](https://x.com/neatlogs)): traces the agent fleet and detects failures.
- **Entire** ([@EntireHQ](https://x.com/EntireHQ), with the `entire-graph` plugin): records every AI session as a checkpoint and maps the code graph.
- **cfo.ai** ([@arithecfo](https://x.com/arithecfo)): models the unit economics and the business plan.

Licensed under the [MIT License](LICENSE).

AI involvement is tracked per commit, not hidden. Each AI-made commit carries an `Entire-Checkpoint:` trailer that links it to the full session transcript (`entire checkpoint explain <id>`, `entire why <file>:<line>`). Autonomous repairs also carry signed `Kintsugi-*` trailers.

#neatHack
