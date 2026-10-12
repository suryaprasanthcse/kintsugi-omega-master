"""The agent under test: Omega's root-cause localisation (the Archivist's step 1, Stage 12).

``diagnose`` asks ``kintsugi.crew.archivist.localise`` for the regions behind a symptom at the
case's HEAD and keeps the top three: entire-graph search where it is installed, the hunks of
recent commits otherwise. It calls no model, so a replay costs nothing; the model-assisted
extraction that follows in a real incident is outside this measure.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from kintsugi.crew.archivist import localise

TOP = 3


def diagnose(repo: Path, symptom: str) -> dict[str, Any]:
    started = time.monotonic()
    regions, _context, method = localise(Path(repo), symptom)
    suspects = [{"file": str(r["file_path"]).replace("\\", "/"), "start": int(r["start_line"]),
                 "end": int(r["end_line"]),
                 "symbol": r.get("qualified_name") or r.get("symbol_name")}
                for r in regions[:TOP]]  # fmt: skip
    return {"suspects": suspects, "method": method, "cost_usd": 0.0,
            "seconds": round(time.monotonic() - started, 3)}  # fmt: skip


def offline_model(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Omega's replay calls no model; HostSpec requires the entry point."""
    raise NotImplementedError("the omega host's replay never calls a model")
