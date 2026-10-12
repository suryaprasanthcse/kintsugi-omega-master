"""O3: replay mode (Stage 10): traces the maintainers exported, read like live neatlogs traces.

An export directory holds one folder per trace with the neatlogs Public API's own answers:
``trace.json`` (``traces.get``), ``spans.json`` (``traces.spans``) and ``raw_spans.json``
(``traces.raw_spans``), as full responses or their ``data``. ``ExportedTraces`` answers the
same calls ``senses.records.load_trace`` makes, so a replayed trace becomes the same
``TraceRecord`` and ``FailureObservation`` a live one would; Omega never calls the
maintainers' tracing account.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kintsugi.senses.neatlogs_api import PayloadField, RawSpan, Span, TraceSummary
from kintsugi.senses.records import TraceRecord, load_trace
from kintsugi.senses.signature import FailureObservation, from_trace


class ExportError(ValueError):
    pass


def _data(path: Path) -> Any:
    if not path.is_file():
        raise ExportError(f"{path} is missing from the export")
    raw = json.loads(path.read_text(encoding="utf-8"))
    body = raw.get("body", raw) if isinstance(raw, dict) else raw
    return body.get("data", body) if isinstance(body, dict) else body


class ExportedTraces:
    """The ``load_trace`` port over an export directory."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def ids(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir() if (p / "trace.json").is_file())

    def trace(self, trace_id: str) -> TraceSummary:
        return TraceSummary.model_validate(_data(self.folder / trace_id / "trace.json"))

    def _list(self, trace_id: str, name: str) -> list[dict[str, Any]]:
        data = _data(self.folder / trace_id / name)
        return list(data.get("spans", []) if isinstance(data, dict) else data)

    def spans(self, trace_id: str) -> list[Span]:
        return [Span.model_validate(s) for s in self._list(trace_id, "spans.json")]

    def raw_spans(self, trace_id: str) -> list[RawSpan]:
        return [RawSpan.model_validate(s) for s in self._list(trace_id, "raw_spans.json")]

    def span_payload(
        self, trace_id: str, span_id: str, fields: Sequence[str] = ("input", "output", "error")
    ) -> dict[str, PayloadField]:
        raise ExportError("replay mode reads previews only; export full payloads to replay them")

    def records(self) -> list[TraceRecord]:
        return [load_trace(self, tid) for tid in self.ids()]


def observations(records: Sequence[TraceRecord], host: str) -> list[FailureObservation]:
    found = []
    for record in records:
        obs = from_trace(record, ["onboard:replay"])
        if obs is not None:
            found.append(obs if obs.host == host else _with_host(obs, host))
    return found


def _with_host(obs: FailureObservation, host: str) -> FailureObservation:
    from dataclasses import replace

    return replace(obs, host=host)
