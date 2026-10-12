"""O6: the immune scan (Stage 10): the antibody library's motifs over an onboarded checkout.

The checkout's graph comes from entire-graph (local, no network) and the motif engine's own
host graph (``memory.predicates.host_graph``); every ACTIVE antibody's motif is matched against
it. A match is a finding for the maintainers (the binding names the code), not a change: any fix
goes through the gates and arrives as a pull request. Without entire-graph the scan is skipped
and says so.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kintsugi.memory import graph, predicates
from kintsugi.memory import motif as m
from kintsugi.memory.antibody import AntibodyState
from kintsugi.memory.library import AntibodyLibrary


def scan(
    checkout: Path,
    library: AntibodyLibrary,
    *,
    package_dir: str,
    side_effect_tools: Iterable[str] = (),
    cache: Path | None = None,
) -> dict[str, Any]:
    try:
        snapshot = graph.snapshot(checkout)
    except (graph.GraphError, OSError) as error:
        return {"status": "skipped", "reason": f"entire-graph unavailable: {error}"[:300],
                "matches": []}  # fmt: skip
    tools = list(side_effect_tools)
    g = predicates.host_graph(snapshot, package_dir=package_dir, side_effect_tools=tools,
                              source_root=checkout)  # fmt: skip
    registry = predicates.registry(
        classify=None, cache=predicates.VerdictCache(cache or checkout / ".onboard-verdicts.json")
    )
    matches = []
    for ab_id in library.ids():
        ab = library.load(ab_id)
        if ab.state is not AntibodyState.ACTIVE:
            continue
        for embedding in m.MotifMatcher(ab.motif, predicates=registry).matches(g):
            matches.append({"antibody": ab.ref, "binding": predicates.names(g, embedding)})
    return {"status": "scanned", "antibodies": len(library.ids()), "matches": matches}
