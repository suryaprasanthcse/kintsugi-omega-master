"""Onboarding O1–O7 for Repair Clinic and open-source hosts (Stage 10, spec §7.6).

Everything here works on a local checkout and on traces the maintainers exported; nothing
reaches a foreign repository or service. ``pipeline.onboard`` runs the steps in order:

- O1 ``intake``: the Clinic issue form, its consent boxes all checked, the repository slug.
- O2 ``instrument``: a static health card of the checkout (agent calls, tools, retries,
  side-effecting HTTP, tests, CI), and the tracing it lacks.
- O3 ``replay_mode``: the maintainers' exported traces read through the same path as live
  neatlogs traces (``senses.records.load_trace``), so the crew sees ordinary observations.
- O4 ``eval_synth``: seed eval cases from the failing traces (input, expected failure class).
- O5 ``baseline``: the pass rate of the exported traces with its Wilson interval.
- O6 ``immune_scan``: the antibody library's motifs over the checkout's graph.
- O7 the receipt: pull requests only on a foreign repository (``ShipConfig.pr_only``), never a
  merge; every foreign post waits for the Architect's approval.
"""
