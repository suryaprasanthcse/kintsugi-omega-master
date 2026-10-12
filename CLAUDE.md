<!-- entire-graph:begin -->
This repo has the entire-graph code graph installed. Before exploring code with
grep/find/whole-file reads, read .entire/graph-agent.md — resolution-first guidance
for using graph retrieval, focused source inspection, and verification.
@.entire/graph-agent.md
<!-- entire-graph:end -->

## Verified CLI Surface (Entire 0.11.4 / entire-graph 0.4.0)

Checked against the installed CLIs. Where these differ from `Blueprint_Omega_Master.md` (§4.3, Appendix D), these win.

- `entire why <file>:<line>` (labs) — commit, checkpoint, prompt and session behind a line. Use for intent recovery (§4.3.2 steps 2–3).
- `entire checkpoint explain <id|sha>` — replaces top-level `entire explain`.
- `entire session resume <branch>` — replaces top-level `entire resume`.
- No `rewind` command exists (`entire rewind` / `entire checkpoint rewind` are absent).
- Python constructor calls are `CONSTRUCTS`, not `CALLS`: motif edge `[F, S, CONSTRUCTS]`. Python has no `READS_FIELD` / `WRITES_FIELD` / `ACCESSES` / `IMPLEMENTS`; feature-detect with `entire graph capabilities --json`.

## Repository and AI Tooling Disclosure

- This is the **Master Edition** (`suryaprasanthcse/kintsugi-omega-master`). The #neatHack submission `suryaprasanthcse/kintsugi-omega` is frozen at `13a744b`: never fetch into, commit to, push to, or add a remote for it. The plan is `MASTER_HANDOFF.md`.
- Surya Prasanth R (`@suryaprasanthcse`) is the sole human architect and the sole repository contributor. Every commit is authored as `Surya Prasanth R <suryaprasanthcse@gmail.com>`.
- AI tooling, disclosed in `README.md`: **Gideon (Google Gemini)** for strategic systems architecture, cryptographic audit design and operational direction (no code, no commits); **Claude Code** for implementation and headless agent crew execution under the architect's direction; **neatlogs** (`@neatlogs`), **Entire** (`@EntireHQ`) and **cfo.ai** (`@arithecfo`) as the platform.
- Never send this edition's telemetry to the #neatHack neatlogs project: use a separate neatlogs project and tokens in this clone's `.env`.

## Commit Rules

- NEVER include `Co-authored-by:` trailers in any git commit message (human or headless Surgeon). Authorship attribution is tracked exclusively via `Entire-Checkpoint:` and `Kintsugi-*` trailers.
- Commit and push with `ENTIRE_TEST_TTY=0` (set in `.claude/settings.json`) so Entire's Windows `prepare-commit-msg` hook links the checkpoint instead of blocking on a hidden console prompt.
