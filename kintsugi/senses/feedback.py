"""Write-back to neatlogs traces (spec §4.2): the culprit span and the repair's verdict.

- **Culprit** (after attribution): ``PUT /traces/{traceId}/spans/{spanId}/feedback`` with a
  ``down`` vote and the incident's diagnosis on the span the failure observation points at (the
  first failed tool span, else the grader's verdict span, as ``signature.from_trace`` picks it).
- **Verdict** (after the canary resolves the incident): ``POST /traces/{traceId}/feedback`` on
  each failing trace, rated +100 with the incident, the repair's PR and the error class as the
  issue type, so the trace history shows the failure was repaired and how.

Both operations need an OAuth user token (``NEATLOGS_OAUTH_TOKEN``, A-29). Feedback is
best-effort evidence, never a step's outcome: every call is idempotent (a key per trace, span
and incident), and a missing credential or an API error is recorded in the result instead of
failing the incident.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from kintsugi.senses.neatlogs_api import ApiError, CredentialUnavailable
from kintsugi.senses.records import VERDICT_SPAN, TraceRecord, load_trace

NOTE_LIMIT = 2000  # span notes hold at most 2048 UTF-8 bytes


def _key(*parts: str) -> str:
    """One logical write, one key: unpadded base64url SHA-256 (the API refuses other formats
    but UUIDv4, A-35)."""
    material = "\x1f".join(["kintsugi.feedback/v1", *parts]).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(material).digest()).decode().rstrip("=")


def culprit_span(record: TraceRecord) -> Any:
    """The span a failure observation names: the first failed tool span, else the verdict."""
    spans = sorted(record.spans, key=lambda s: s.started_at)
    failing = next((s for s in spans if s.failed and s.kind == "TOOL"), None)
    if failing is None:
        failing = next((s for s in spans if s.name == VERDICT_SPAN), None)
    return failing


@dataclass
class TraceFeedback:
    api: Any  # kintsugi.senses.neatlogs_api.NeatlogsApi

    def _send(self, call: Any, *args: Any, **kwargs: Any) -> str:
        try:
            call(*args, **kwargs)
        except CredentialUnavailable as error:
            return f"skipped: {error}"[:300]
        except ApiError as error:
            return f"failed: {error}"[:300]
        return "sent"

    def culprit(self, incident: str, trace_ids: Iterable[str], diagnosis: str
                ) -> list[dict[str, Any]]:  # fmt: skip
        results = []
        for trace_id in list(trace_ids)[:20]:
            try:
                span = culprit_span(load_trace(self.api, trace_id))
            except (CredentialUnavailable, ApiError) as error:
                results.append({"trace": trace_id, "status": f"failed: {error}"[:300]})
                continue
            if span is None:
                results.append({"trace": trace_id, "status": "skipped: no failing span"})
                continue
            note = f"{incident}: {diagnosis}"[:NOTE_LIMIT]
            status = self._send(self.api.span_feedback, trace_id, span.span_id, "down",
                                note=note,
                                idempotency_key=_key(trace_id, span.span_id, incident))  # fmt: skip
            results.append({"trace": trace_id, "span": span.span_id, "status": status})
        return results

    def verdict(self, incident: str, trace_ids: Iterable[str], *, pr_url: str | None,
                error_class: str | None) -> list[dict[str, Any]]:  # fmt: skip
        comment = f"Repaired by KINTSUGI-Omega incident {incident}" + (
            f": {pr_url}" if pr_url else "")  # fmt: skip
        body: dict[str, Any] = {"rating": 100, "comment": comment}
        if error_class:
            body["issueTypes"] = [error_class[:100]]
        results = []
        for trace_id in list(trace_ids)[:20]:
            status = self._send(self.api.trace_feedback, trace_id, body,
                                idempotency_key=_key(trace_id, incident, "verdict"))  # fmt: skip
            results.append({"trace": trace_id, "status": status})
        return results
