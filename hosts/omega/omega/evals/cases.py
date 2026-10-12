"""The replay suite: incidents recorded in the Stage 6–9 rehearsals (Stage 12, spec §6.9).

Each case is rebuilt exactly from committed sources (fixtures, host code), so a replay is the
same input every time. The ground truth is the code the incident's own record names: the
Stage 6 repair's region and AB-1's binding (``antibodies/AB-1``), the Stage 7 vaccination
bindings on CRM practice and Scout, and the Stage 9 treatment's step. ``symptom_source`` says
where each symptom text comes from: the recorded signature label, or the recorded verdict class
described in words for the replay.

The plan calls for 20 recorded incidents; the rehearsals so far recorded these 4 distinct ones
(the first Stage 6 rehearsal, INC-REH-0001, is the same scenario as INC-R235459-0001). New
incidents recorded in W or the Stage 13b dress rehearsal are appended here, never invented.
"""

from __future__ import annotations

from typing import Any

TARGET_SIZE = 20

CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "INC-R235459-0001",
        "stage": 6,
        "evidence": "evidence/rehearsals/reh-20261011-235459.json",
        "instances": ["INC-REH-0001 (evidence/rehearsals/reh-20261011-232114.json)"],
        "build": "ledger-practice-regression",
        "symptom": "Duplicate books:/lines write after fx_rate lookup",
        "symptom_source": "recorded signature label",
        "truth": [{"file": "practice_ledger/reconcile.py", "symbol": "reconcile"}],
        "recorded": {"cost_usd": 0.665376, "duration_s": 554.3},
    },
    {
        "id": "VAC-AB-1-crm-practice",
        "stage": 7,
        "evidence": "evidence/rehearsals/l3-20261012-074523.json",
        "instances": [],
        "build": "crm-practice",
        "symptom": "FAIL:DUPLICATE_CONTACT: a contact is pushed to the CRM twice when the geo "
        "lookup answers 429 mid-batch",
        "symptom_source": "recorded verdict class, described for the replay",
        "truth": [{"file": "practice_crm/sync.py", "symbol": "sync_contacts"}],
        "recorded": {"cost_usd": 0.08905, "duration_s": None},
    },
    {
        "id": "VAC-AB-1-scout",
        "stage": 7,
        "evidence": "evidence/rehearsals/l3-20261012-074523.json",
        "instances": [],
        "build": "scout",
        "symptom": "FAIL:RATE_LIMITED: after a 429 a document's pages are fetched again from "
        "page one until the retries run out",
        "symptom_source": "recorded verdict class, described for the replay",
        "truth": [{"file": "scout/research.py", "symbol": "fetch_all_pages"}],
        "recorded": {"cost_usd": 0.104426, "duration_s": None},
    },
    {
        "id": "INC-L5MEASURED-0001",
        "stage": 9,
        "evidence": "evidence/rehearsals/l5-20261012-200248.json",
        "instances": [],
        "build": "triage",
        "symptom": "Wrong component labels on issues whose titles are in another language",
        "symptom_source": "recorded failure set (mixed-language live issues), described",
        "truth": [{"file": "triage/tools.py", "symbol": "classify_component"}],
        "recorded": {"cost_usd": 0.0, "duration_s": None},
    },
)
