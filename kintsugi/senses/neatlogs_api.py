"""neatlogs Public API client (spec §4.1.6, §4.2, Appendix C).

A thin adapter over the pinned contract (``kintsugi.senses.contract``): every call names a pinned
operation, so its method, path, accepted credentials and idempotency rule come from the contract,
not from this file. Verified behaviour (Stage 4 spikes, ``docs/SPIKES.md``):

- **Credentials.** A service-account token (``nlsa_…``) is sent as ``Authorization: Bearer``
  with ``x-project-id``. The SDK ingest key is refused (401). Some operations accept only an OAuth
  user token (``nloa_…``): webhook test and delivery replay, detection preview, generate and
  backfills. Without ``NEATLOGS_OAUTH_TOKEN`` they raise ``CredentialUnavailable`` before any
  request is made, and callers fall back (the poller is the source of truth, spec §4.1.7).
- **Envelope.** Success is ``{"success": true, "requestId", "data"}``; errors are RFC 9457
  problem details with ``code``, ``retryable`` and ``requestId``.
- **Rate limits** are per operation class (``standard-read`` 300, ``standard-write`` 60,
  ``search-analytics`` 30, ``export-import-outbound`` 10, ``metadata`` 120), each a token bucket
  that refills over 60 s. A 429 carries ``Retry-After``; the client waits at least that long,
  then backs off exponentially with jitter (spike A11).
- **Writes** need an ``Idempotency-Key``; a retry reuses the key of the original request.
- **Cursors** are opaque and expire 15 minutes after a chain starts
  (``400 PUBLIC_API_INVALID_CURSOR`` → ``CursorExpired``); a caller restarts the list.

Credentials never appear in ``repr``, logs or exceptions.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from kintsugi.senses import contract

BASE_URL = "https://app.neatlogs.com/api/v1/public"
RETRYABLE_CODES = frozenset({"RATE_LIMITED", "IDEMPOTENCY_REQUEST_IN_PROGRESS"})


# --------------------------------------------------------------------------- credentials


@dataclass(frozen=True)
class Credentials:
    project_id: str
    service_token: str | None = field(default=None, repr=False)
    oauth_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Credentials:
        project = env.get("NEATLOGS_PROJECT_ID", "").strip()
        if not project:
            raise CredentialUnavailable("NEATLOGS_PROJECT_ID is not set")
        return cls(
            project_id=project,
            service_token=env.get("NEATLOGS_SERVICE_TOKEN") or None,
            oauth_token=env.get("NEATLOGS_OAUTH_TOKEN") or None,
        )

    def token_for(self, op: contract.Operation) -> str:
        if self.service_token and "ServiceAccountBearerAuth" in op.security:
            return self.service_token
        if self.oauth_token and "OAuth2" in op.security:
            return self.oauth_token
        needed = " or ".join(op.security) or "a credential"
        raise CredentialUnavailable(f"{op.name} needs {needed}; none is configured")


# --------------------------------------------------------------------------- errors


class CredentialUnavailable(RuntimeError):
    """The operation needs a credential type that is not configured (no request was sent)."""


class ApiError(RuntimeError):
    def __init__(
        self,
        op: str,
        status: int,
        code: str,
        detail: str,
        *,
        request_id: str | None = None,
        retryable: bool = False,
        errors: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(f"{op}: HTTP {status} {code}: {detail} (request {request_id})")
        self.op = op
        self.status = status
        self.code = code
        self.detail = detail
        self.request_id = request_id
        self.retryable = retryable
        self.errors = list(errors)


class RateLimited(ApiError):
    pass


class CursorExpired(ApiError):
    pass


class Conflict(ApiError):
    """409: a revision or state conflict; read current state before writing again."""


class OperationUnavailable(ApiError):
    """404 for an operation behind a disabled capability gate (e.g. ``tier-c-compute``)."""


# --------------------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True
    )


class Page(_Model):
    has_more: bool
    limit: int
    next_cursor: str | None


class TraceSummary(_Model):
    trace_id: str
    trace_name: str | None
    workflow_name: str | None
    session_id: str
    status: str
    tags: tuple[str, ...]
    started_at: datetime
    completed_at: datetime | None = None
    ingested_at: datetime | None = None
    duration_ms: float
    total_cost_usd: float
    total_tokens: int
    tool_calls_count: int
    llm_calls_count: int
    spans_count: int
    finalization_status: str | None = None
    metadata_revision: int | None = None


class Span(_Model):
    trace_id: str
    span_id: str
    parent_span_id: str | None
    span_name: str
    span_type: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: float
    cost_usd: float
    model: str | None
    provider: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class Preview(_Model):
    mime_type: str | None
    preview: str
    truncated: bool


class ErrorPreview(_Model):
    message_preview: str
    truncated: bool
    type: str | None


class RawSpan(_Model):
    trace_id: str
    span_id: str
    parent_span_id: str | None
    span_name: str
    span_type: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: float
    input: Preview
    output: Preview
    error: ErrorPreview


class PayloadField(_Model):
    available: bool
    mime_type: str | None
    truncated: bool
    value: str | None


class Hit(_Model):
    id: str
    detected_at: datetime
    trace_id: str
    span_id: str
    score: float
    item_index: int | None = None


class Detection(_Model):
    id: str
    display_name: str
    slug: str
    type: str
    is_active: bool
    group_id: str
    group_name: str | None = None
    threshold: float
    created_at: datetime
    updated_at: datetime


class Webhook(_Model):
    id: str
    name: str
    enabled: bool
    event_types: tuple[str, ...]
    destination_configured: bool
    created_at: datetime
    updated_at: datetime


class HttpStatus(_Model):
    code: int
    # "class" is a keyword: read it through the alias only.
    status_class: str | None = None


class Delivery(_Model):
    id: str
    attempt: int
    delivered_at: datetime
    event_type: str
    http_status: HttpStatus | None
    latency_ms: int | None
    outcome: str


class AlertRule(_Model):
    id: str
    name: str
    rule_type: str
    enabled: bool
    severity: str
    detection_id: str | None = None
    detection_name: str | None = None
    threshold_count: int | None = None
    window_minutes: int | None = None
    cooldown_minutes: int | None = None


class AlertFire(_Model):
    id: str
    rule_id: str
    rule_name: str
    fired_at: datetime
    detection_name: str | None = None
    count: int | None = None
    sample_trace_ids: tuple[str, ...] = ()
    is_followup: bool = False


@dataclass(frozen=True)
class RateLimitState:
    policy: str
    limit: int | None
    remaining: int | None
    reset_at: int | None  # unix seconds when the bucket is full again
    observed_at: float


def _iso(value: datetime | str | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- client


class NeatlogsApi:
    def __init__(
        self,
        credentials: Credentials,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 30.0,
        max_attempts: int = 5,
        max_delay_s: float = 60.0,
        validate: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.credentials = credentials
        self.max_attempts = max_attempts
        self.max_delay_s = max_delay_s
        self.validate = validate
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._client = httpx.Client(base_url=base_url, transport=transport, timeout=timeout_s)
        self.rate_limits: dict[str, RateLimitState] = {}
        self.calls: dict[str, int] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NeatlogsApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"NeatlogsApi(project={self.credentials.project_id!r})"

    # ----------------------------------------------------------------------- core

    def call(
        self,
        name: str,
        *,
        path: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Call a pinned operation and return the envelope's ``data``."""
        op = contract.operation(name)
        token = self.credentials.token_for(op)
        url = op.path.format(**{k: quote(str(v), safe="") for k, v in (path or {}).items()})
        headers = {
            "Authorization": f"Bearer {token}",
            "x-project-id": self.credentials.project_id,
            "accept": "application/json",
        }
        if op.idempotency_key:
            headers["idempotency-key"] = idempotency_key or str(uuid.uuid4())
        if body is not None and self.validate:
            contract.validate_request(name, body)
        query = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(1, self.max_attempts + 1):
            self.calls[name] = self.calls.get(name, 0) + 1
            try:
                response = self._client.request(
                    op.method.upper(), url, params=query, json=body, headers=headers
                )
            except httpx.TransportError:
                # Safe to repeat: reads are idempotent and writes carry the same key.
                if attempt == self.max_attempts:
                    raise
                self._sleep(self._backoff(attempt, None))
                continue
            self._observe(response)
            if response.status_code < 400:
                return self._data(name, response)
            error = self._error(name, response)
            if self._should_retry(error) and attempt < self.max_attempts:
                self._sleep(self._backoff(attempt, response.headers.get("retry-after")))
                continue
            raise error
        raise AssertionError("unreachable")  # pragma: no cover

    def paginate(
        self,
        name: str,
        items: str,
        *,
        path: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        limit: int | None = None,
        max_items: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Follow ``data.page.nextCursor`` until ``hasMore`` is false (or ``max_items``)."""
        cursor: str | None = None
        yielded = 0
        while True:
            data = self.call(name, path=path, params={**(params or {}), "limit": limit,
                                                      "cursor": cursor})  # fmt: skip
            for item in data[items]:
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            page = Page.model_validate(data["page"])
            if not page.has_more or not page.next_cursor:
                return
            cursor = page.next_cursor

    def _data(self, name: str, response: httpx.Response) -> Any:
        payload = response.json()
        if self.validate:
            contract.validate_response(name, response.status_code, payload)
        if not isinstance(payload, Mapping) or payload.get("success") is not True:
            raise ApiError(name, response.status_code, "MALFORMED_ENVELOPE", "no success envelope")
        return payload.get("data")

    def _error(self, name: str, response: httpx.Response) -> ApiError:
        problem: Mapping[str, Any] = {}
        if "json" in response.headers.get("content-type", ""):
            try:
                parsed = response.json()
                problem = parsed if isinstance(parsed, Mapping) else {}
            except ValueError:
                problem = {}
        code = str(problem.get("code") or f"HTTP_{response.status_code}")
        kwargs: dict[str, Any] = {
            "request_id": problem.get("requestId") or response.headers.get("x-request-id"),
            "retryable": bool(problem.get("retryable", response.status_code in (429, 503))),
            "errors": problem.get("errors") or (),
        }
        detail = str(problem.get("detail") or problem.get("title") or response.reason_phrase)
        status = response.status_code
        cls: type[ApiError] = ApiError
        if status == 429:
            cls = RateLimited
        elif status == 400 and code == "PUBLIC_API_INVALID_CURSOR":
            cls = CursorExpired
        elif status == 409 and code not in RETRYABLE_CODES:
            cls = Conflict
        elif status == 404 and "OPERATION_DISABLED" in code:
            cls = OperationUnavailable
        return cls(name, status, code, detail, **kwargs)

    @staticmethod
    def _should_retry(error: ApiError) -> bool:
        if error.status == 429 or error.code in RETRYABLE_CODES:
            return True
        return error.status in (500, 502, 503, 504) and error.retryable

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        base = min(self.max_delay_s, 0.5 * 2 ** (attempt - 1))
        floor = 0.0
        if retry_after is not None:
            try:
                floor = float(retry_after)
            except ValueError:
                floor = 0.0
        delay = max(floor, base) + self._rng.uniform(0, 0.25 * base)
        return min(self.max_delay_s, delay)

    def _observe(self, response: httpx.Response) -> None:
        policy = response.headers.get("x-neatlogs-ratelimit-policy")
        if policy:
            self.rate_limits[policy] = RateLimitState(
                policy=policy,
                limit=_int(response.headers.get("ratelimit-limit")),
                remaining=_int(response.headers.get("ratelimit-remaining")),
                reset_at=_int(response.headers.get("ratelimit-reset")),
                observed_at=time.time(),
            )

    # ----------------------------------------------------------------------- traces

    def project(self) -> dict[str, Any]:
        return self.call("project.get")

    def traces(
        self,
        *,
        workflow: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 50,
        max_items: int | None = None,
    ) -> Iterator[TraceSummary]:
        params = {"workflowName": workflow, "status": status, "sessionId": session_id,
                  "from": _iso(start), "to": _iso(end)}  # fmt: skip
        for item in self.paginate("traces.list", "traces", params=params, limit=limit,
                                  max_items=max_items):  # fmt: skip
            yield TraceSummary.model_validate(item)

    def trace(self, trace_id: str) -> TraceSummary:
        return TraceSummary.model_validate(self.call("traces.get", path={"traceId": trace_id}))

    def patch_trace(
        self,
        trace_id: str,
        *,
        expected_revision: int,
        tags: Sequence[str] | None = None,
        trace_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> TraceSummary:
        body: dict[str, Any] = {"expectedRevision": expected_revision}
        if tags is not None:
            body["tags"] = list(tags)
        if trace_name is not None:
            body["traceName"] = trace_name
        data = self.call("traces.patch", path={"traceId": trace_id}, body=body,
                         idempotency_key=idempotency_key)  # fmt: skip
        return TraceSummary.model_validate(data)

    def spans(self, trace_id: str) -> list[Span]:
        items = self.paginate("traces.spans", "spans", path={"traceId": trace_id}, limit=50)
        return [Span.model_validate(item) for item in items]

    def raw_spans(self, trace_id: str) -> list[RawSpan]:
        items = self.paginate("traces.raw_spans", "spans", path={"traceId": trace_id}, limit=50)
        return [RawSpan.model_validate(item) for item in items]

    def span_payload(
        self, trace_id: str, span_id: str, fields: Sequence[str] = ("input", "output", "error")
    ) -> dict[str, PayloadField]:
        data = self.call("traces.span_payload", path={"traceId": trace_id, "spanId": span_id},
                         params={"fields": ",".join(fields)})  # fmt: skip
        return {k: PayloadField.model_validate(v) for k, v in data["fields"].items()}

    def export_trace(self, trace_id: str) -> dict[str, Any]:
        return self.call("traces.export", path={"traceId": trace_id}, params={"format": "json"})

    def sharing(self, trace_id: str) -> dict[str, Any]:
        return self.call("traces.sharing", path={"traceId": trace_id})

    def session(self, session_id: str) -> dict[str, Any]:
        return self.call("sessions.get", path={"sessionId": session_id})

    def export_session(self, session_id: str) -> dict[str, Any]:
        return self.call("sessions.export", path={"sessionId": session_id},
                         params={"format": "json"})  # fmt: skip

    def search_traces(self, body: Mapping[str, Any], *, semantic: bool = False) -> dict[str, Any]:
        return self.call("search.semantic" if semantic else "search.traces", body=dict(body))

    def trace_feedback(
        self, trace_id: str, body: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.call("traces.feedback", path={"traceId": trace_id}, body=dict(body),
                         idempotency_key=idempotency_key)  # fmt: skip

    def span_feedback(
        self, trace_id: str, span_id: str, vote: str, *, note: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """§4.2: mark a span (the culprit) ``up`` or ``down``, with a short note."""
        body: dict[str, Any] = {"vote": vote}
        if note is not None:
            body["note"] = note
        return self.call("traces.span_feedback", path={"traceId": trace_id, "spanId": span_id},
                         body=body, idempotency_key=idempotency_key)  # fmt: skip

    # ----------------------------------------------------------------------- detections

    def detection_groups(self) -> list[dict[str, Any]]:
        return list(self.paginate("detection_groups.list", "groups", limit=100))

    def create_detection_group(self, name: str, *, idempotency_key: str | None = None) -> dict:
        return self.call("detection_groups.create", body={"name": name},
                         idempotency_key=idempotency_key)  # fmt: skip

    def detections(self, *, group_id: str | None = None) -> list[Detection]:
        items = self.paginate("detections.list", "detections", params={"groupId": group_id},
                              limit=100)  # fmt: skip
        return [Detection.model_validate(item) for item in items]

    def create_detection(
        self, body: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> Detection:
        data = self.call("detections.create", body=dict(body), idempotency_key=idempotency_key)
        return Detection.model_validate(data)

    def set_detection_status(
        self, detection_id: str, active: bool, *, idempotency_key: str | None = None
    ) -> Detection:
        data = self.call("detections.set_status", path={"detectionId": detection_id},
                         body={"isActive": active}, idempotency_key=idempotency_key)  # fmt: skip
        return Detection.model_validate(data)

    def hit_pages(self, detection_id: str, *, limit: int = 100) -> Iterator[tuple[list[Hit], Page]]:
        """Hits page by page, newest first (one cursor chain; it expires after 15 minutes)."""
        cursor: str | None = None
        while True:
            data = self.call("detections.hits", path={"detectionId": detection_id},
                             params={"limit": limit, "cursor": cursor})  # fmt: skip
            page = Page.model_validate(data["page"])
            yield [Hit.model_validate(item) for item in data["hits"]], page
            if not page.has_more or not page.next_cursor:
                return
            cursor = page.next_cursor

    def hits(self, detection_id: str, *, max_items: int | None = None) -> Iterator[Hit]:
        items = self.paginate("detections.hits", "hits", path={"detectionId": detection_id},
                              limit=100, max_items=max_items)  # fmt: skip
        for item in items:
            yield Hit.model_validate(item)

    def detection_trends(self, detection_id: str, **params: Any) -> dict[str, Any]:
        return self.call("detections.trends", path={"detectionId": detection_id}, params=params)

    def detection_history(self, detection_id: str) -> list[dict[str, Any]]:
        return list(self.paginate("detections.history", "events",
                                  path={"detectionId": detection_id}, limit=100))  # fmt: skip

    # ----------------------------------------------------------------------- alerts, webhooks

    def alert_rules(self) -> list[AlertRule]:
        items = self.paginate("alert_rules.list", "alertRules", limit=100)
        return [AlertRule.model_validate(item) for item in items]

    def create_alert_rule(
        self, body: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> AlertRule:
        data = self.call("alert_rules.create", body=dict(body), idempotency_key=idempotency_key)
        return AlertRule.model_validate(data)

    def set_alert_rule_status(
        self, rule_id: str, enabled: bool, *, idempotency_key: str | None = None
    ) -> AlertRule:
        data = self.call("alert_rules.set_status", path={"alertRuleId": rule_id},
                         body={"enabled": enabled}, idempotency_key=idempotency_key)  # fmt: skip
        return AlertRule.model_validate(data)

    def alert_history(
        self,
        *,
        rule_id: str | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> list[AlertFire]:
        params = {"alertRuleId": rule_id, "startTime": _iso(start), "endTime": _iso(end)}
        items = self.paginate("alert_history.list", "alertHistory", params=params, limit=100)
        return [AlertFire.model_validate(item) for item in items]

    def webhooks(self) -> list[Webhook]:
        data = self.call("webhooks.list")
        return [Webhook.model_validate(item) for item in data["webhooks"]]

    def create_webhook(
        self,
        name: str,
        destination_url: str,
        event_types: Sequence[str],
        *,
        secret: str | None = None,
        idempotency_key: str | None = None,
    ) -> Webhook:
        body: dict[str, Any] = {"name": name, "destinationUrl": destination_url,
                                "eventTypes": list(event_types)}  # fmt: skip
        if secret is not None:
            body["secret"] = secret
        data = self.call("webhooks.create", body=body, idempotency_key=idempotency_key)
        return Webhook.model_validate(data)

    def update_webhook(
        self, webhook_id: str, body: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> Webhook:
        data = self.call("webhooks.update", path={"webhookId": webhook_id}, body=dict(body),
                         idempotency_key=idempotency_key)  # fmt: skip
        return Webhook.model_validate(data)

    def set_webhook_status(
        self, webhook_id: str, enabled: bool, *, idempotency_key: str | None = None
    ) -> Webhook:
        data = self.call("webhooks.set_status", path={"webhookId": webhook_id},
                         body={"enabled": enabled}, idempotency_key=idempotency_key)  # fmt: skip
        return Webhook.model_validate(data)

    def deliveries(self, webhook_id: str, *, max_items: int | None = None) -> list[Delivery]:
        items = self.paginate("webhooks.deliveries", "deliveries", path={"webhookId": webhook_id},
                              limit=100, max_items=max_items)  # fmt: skip
        return [Delivery.model_validate(_delivery(item)) for item in items]

    def replay_delivery(
        self, webhook_id: str, delivery_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """OAuth only (spike A6): raises ``CredentialUnavailable`` without an OAuth token."""
        return self.call("webhooks.replay", path={"webhookId": webhook_id,
                                                  "deliveryId": delivery_id},
                         body={}, idempotency_key=idempotency_key)  # fmt: skip

    def test_webhook(self, webhook_id: str, *, idempotency_key: str | None = None) -> dict:
        return self.call("webhooks.test", path={"webhookId": webhook_id}, body={},
                         idempotency_key=idempotency_key)  # fmt: skip


def _delivery(item: Mapping[str, Any]) -> dict[str, Any]:
    status = item.get("httpStatus")
    if isinstance(status, Mapping):
        return {**item, "httpStatus": {"code": status.get("code"),
                                       "statusClass": status.get("class")}}  # fmt: skip
    return dict(item)
