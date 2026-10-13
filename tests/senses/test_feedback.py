"""Write-back to neatlogs traces (§4.2): the culprit span after attribution, the verdict after
the canary; best-effort, idempotent, OAuth-only (A-29)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from kintsugi.senses.feedback import TraceFeedback, culprit_span
from kintsugi.senses.neatlogs_api import CredentialUnavailable
from kintsugi.senses.records import load_trace
from tests.senses.test_neatlogs_api import Recorder, make_api
from tests.senses.test_records_and_tags import FixtureApi

TRACE = "d23ffdad100e3a3bd55365ebc266447d"
KEY = re.compile(r"^[A-Za-z0-9_-]{43}$")  # unpadded base64url SHA-256 (A-35)


class FeedbackApi(FixtureApi):
    """The recorded trace plus feedback calls recorded (or refused without OAuth)."""

    def __init__(self, *, oauth: bool = True) -> None:
        super().__init__()
        self.oauth = oauth
        self.sent: list[tuple[Any, ...]] = []

    def span_feedback(self, trace_id: str, span_id: str, vote: str, *, note: str | None = None,
                      idempotency_key: str | None = None) -> dict[str, Any]:  # fmt: skip
        if not self.oauth:
            raise CredentialUnavailable("traces.span_feedback needs NEATLOGS_OAUTH_TOKEN")
        self.sent.append(("span", trace_id, span_id, vote, note, idempotency_key))
        return {}

    def trace_feedback(self, trace_id: str, body: dict[str, Any], *,
                       idempotency_key: str | None = None) -> dict[str, Any]:  # fmt: skip
        if not self.oauth:
            raise CredentialUnavailable("traces.feedback needs NEATLOGS_OAUTH_TOKEN")
        self.sent.append(("trace", trace_id, body, idempotency_key))
        return {}


def test_the_culprit_is_the_span_the_failure_observation_names() -> None:
    record = load_trace(FixtureApi(), TRACE)
    span = culprit_span(record)
    assert span is not None
    assert span.name == "grader.verdict"  # a passing recorded trace: no failed tool span


def test_culprit_and_verdict_are_sent_once_per_trace_with_stable_keys() -> None:
    api = FeedbackApi()
    feedback = TraceFeedback(api)
    first = feedback.culprit("INC-0042", [TRACE], "state reset on retry in run_reconcile")
    again = feedback.culprit("INC-0042", [TRACE], "state reset on retry in run_reconcile")
    assert first == again
    assert first[0]["status"] == "sent"
    kind, trace, _span, vote, note, key = api.sent[0]
    assert (kind, trace, vote) == ("span", TRACE, "down")
    assert note.startswith("INC-0042: state reset")
    assert KEY.match(key) and key == api.sent[1][5]  # noqa: PT018 - the same write, the same key
    verdict = feedback.verdict("INC-0042", [TRACE], pr_url="https://example.invalid/pr/5",
                               error_class="kintsugi:phi-ledger")  # fmt: skip
    assert verdict == [{"trace": TRACE, "status": "sent"}]
    _, _, body, vkey = api.sent[-1]
    assert body == {"rating": 100, "issueTypes": ["kintsugi:phi-ledger"],
                    "comment": "Repaired by KINTSUGI-Omega incident INC-0042: "
                               "https://example.invalid/pr/5"}  # fmt: skip
    assert KEY.match(vkey) and vkey != key  # noqa: PT018


def test_without_an_oauth_token_feedback_is_skipped_never_raised() -> None:
    feedback = TraceFeedback(FeedbackApi(oauth=False))
    [culprit] = feedback.culprit("INC-0042", [TRACE], "diagnosis")
    assert culprit["status"].startswith("skipped:")
    [verdict] = feedback.verdict("INC-0042", [TRACE], pr_url=None, error_class=None)
    assert verdict["status"].startswith("skipped:")


def test_the_client_puts_span_feedback_with_oauth_and_refuses_without() -> None:
    ok = httpx.Response(200, json={"data": {"callerVote": "down", "downCount": 1,
                                            "spanId": "1c1bd14702021f71", "upCount": 0},
                                   "requestId": "req_" + "0" * 32, "success": True})  # fmt: skip
    recorder = Recorder(ok)
    api = make_api(recorder, oauth="oauth-token")
    api.span_feedback(TRACE, "1c1bd14702021f71", "down", note="INC-0042: culprit",
                      idempotency_key="a" * 43)  # fmt: skip
    [request] = recorder.requests
    assert request.method == "PUT"
    assert request.url.path.endswith(f"/traces/{TRACE}/spans/1c1bd14702021f71/feedback")
    assert request.headers["authorization"] == "Bearer oauth-token"
    assert request.headers["idempotency-key"] == "a" * 43
    assert json.loads(request.content) == {"vote": "down", "note": "INC-0042: culprit"}
    without = make_api(Recorder(), oauth=None)
    feedback = TraceFeedback(without)
    assert feedback.verdict("INC-0042", [TRACE], pr_url=None, error_class=None)[0][
        "status"].startswith("skipped:")  # fmt: skip
