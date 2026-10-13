"""Exported traces from a local run (Stage 10 rehearsal): what a maintainer's export would hold.

A Clinic or open-source host exports its own neatlogs traces for replay mode (O3); for the
disclosed OSS-1 rehearsal the practice host has no neatlogs project, so its traces are captured
locally instead. This module is the host-side driver, run as a child process behind the
rehearsal's fabric (``python -m kintsugi.onboard.trace_export``):

- the neatlogs SDK is started with exporting disabled and an in-memory OpenTelemetry provider,
  so the host's own spans (``task:<id>``, its tools, ``grader.verdict``) are captured and never
  sent anywhere;
- every task runs through ``hostkit.evalrun.run_one``, exactly as an eval run does;
- the captured spans are written as the Public API's own answers, one folder per trace:
  ``trace.json`` (``traces.get``), ``spans.json`` (``traces.spans``) and ``raw_spans.json``
  (``traces.raw_spans``), which ``replay_mode.ExportedTraces`` reads like live traces.

Trace and span ids come from a seeded generator, so one seed always exports the same ids. While
the driver runs, a socket guard refuses every connection that is not to a loopback address and
counts the attempts (the evidence records the count).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import random
import socket
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREVIEW_LIMIT = 4000  # characters of an input/output/error preview before it is truncated


class NonLoopbackRefused(ConnectionError):
    pass


class LoopbackGuard:
    """Refuse (and count) every socket connection to a non-loopback address while active."""

    def __init__(self) -> None:
        self.refused: list[str] = []
        self._original: Any = None

    @staticmethod
    def _loopback(address: Any) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        if not isinstance(host, str):
            return False
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host.split("%")[0]).is_loopback
        except ValueError:
            return False

    def __enter__(self) -> LoopbackGuard:
        self._original = socket.socket.connect
        guard, original = self, self._original

        def connect(sock: socket.socket, address: Any) -> Any:
            if sock.family in (socket.AF_INET, socket.AF_INET6) and not guard._loopback(address):
                guard.refused.append(str(address)[:200])
                raise NonLoopbackRefused(f"offline rehearsal: refused {address!r}")
            return original(sock, address)

        socket.socket.connect = connect  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        socket.socket.connect = self._original  # type: ignore[method-assign]


def _iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    stamp = datetime.fromtimestamp(ns / 1e9, tz=UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def _hex(value: int, width: int) -> str:
    return f"{value:0{width}x}"


def _preview(value: Any, mime: Any) -> dict[str, Any]:
    text = "" if value is None else str(value)
    return {"mimeType": mime if isinstance(mime, str) else None,
            "preview": text[:PREVIEW_LIMIT], "truncated": len(text) > PREVIEW_LIMIT}  # fmt: skip


def _error(span: Any) -> dict[str, Any]:
    for event in span.events:
        if event.name == "exception":
            attrs = dict(event.attributes or {})
            message = str(attrs.get("exception.message", ""))
            return {"messagePreview": message[:PREVIEW_LIMIT],
                    "truncated": len(message) > PREVIEW_LIMIT,
                    "type": attrs.get("exception.type")}  # fmt: skip
    description = span.status.description or ""
    return {"messagePreview": description[:PREVIEW_LIMIT],
            "truncated": len(description) > PREVIEW_LIMIT, "type": None}  # fmt: skip


def _status(span: Any) -> str:
    from opentelemetry.trace import StatusCode

    return "ERROR" if span.status.status_code is StatusCode.ERROR else "OK"


def _ms(span: Any) -> float:
    if span.end_time is None or span.start_time is None:
        return 0.0
    return round((span.end_time - span.start_time) / 1e6, 3)


def _int(attrs: dict[str, Any], key: str) -> int:
    value = attrs.get(key)
    return int(value) if isinstance(value, int | float) else 0


def to_exports(spans: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Group finished OpenTelemetry spans by trace into the Public API's three answers."""
    by_trace: dict[str, list[Any]] = {}
    for span in spans:
        by_trace.setdefault(_hex(span.context.trace_id, 32), []).append(span)
    exports: dict[str, dict[str, Any]] = {}
    for trace_id, members in sorted(by_trace.items()):
        members.sort(key=lambda s: (s.start_time or 0, _hex(s.context.span_id, 16)))
        rows, raws = [], []
        for span in members:
            attrs = dict(span.attributes or {})
            parent = _hex(span.parent.span_id, 16) if span.parent is not None else None
            kind = str(attrs.get("openinference.span.kind", "UNKNOWN")).upper()
            prompt, completion = (_int(attrs, "llm.token_count.prompt"),
                                  _int(attrs, "llm.token_count.completion"))  # fmt: skip
            common = {"traceId": trace_id, "spanId": _hex(span.context.span_id, 16),
                      "parentSpanId": parent, "spanName": span.name, "spanType": kind,
                      "status": _status(span), "startedAt": _iso(span.start_time),
                      "endedAt": _iso(span.end_time), "durationMs": _ms(span)}  # fmt: skip
            rows.append({**common, "costUsd": 0, "model": attrs.get("llm.model_name"),
                         "provider": attrs.get("llm.provider"), "promptTokens": prompt,
                         "completionTokens": completion,
                         "totalTokens": _int(attrs, "llm.token_count.total")
                         or prompt + completion})  # fmt: skip
            raws.append({**common,
                         "input": _preview(attrs.get("input.value"), attrs.get("input.mime_type")),
                         "output": _preview(attrs.get("output.value"),
                                            attrs.get("output.mime_type")),
                         "error": _error(span)})  # fmt: skip
        root = next((s for s in members if s.parent is None), members[0])
        root_attrs = dict(root.attributes or {})
        tags = [t for t in str(root_attrs.get("neatlogs.tags", "")).split(",") if t]
        failed = any(r["status"] == "ERROR" for r in rows if r["parentSpanId"] is None)
        trace = {"traceId": trace_id, "traceName": root.name,
                 "workflowName": root_attrs.get("neatlogs.workflow_name"),
                 "sessionId": trace_id, "status": "ERROR" if failed else "OK", "tags": tags,
                 "startedAt": _iso(root.start_time), "completedAt": _iso(root.end_time),
                 "ingestedAt": None, "durationMs": _ms(root), "totalCostUsd": 0,
                 "totalTokens": sum(r["totalTokens"] for r in rows),
                 "toolCallsCount": sum(r["spanType"] == "TOOL" for r in rows),
                 "llmCallsCount": sum(r["spanType"] == "LLM" for r in rows),
                 "spansCount": len(rows), "finalizationStatus": "finalized",
                 "metadataRevision": 0}  # fmt: skip
        page = {"hasMore": False, "limit": max(50, len(rows)), "nextCursor": None}
        exports[trace_id] = {
            "trace.json": {"operation": "traces.get", "status": 200,
                           "body": {"data": trace, "success": True}},
            "spans.json": {"operation": "traces.spans", "status": 200,
                           "body": {"data": {"page": page, "spans": rows}, "success": True}},
            "raw_spans.json": {"operation": "traces.raw_spans", "status": 200,
                               "body": {"data": {"page": page, "spans": raws},
                                        "success": True}},
        }  # fmt: skip
    return exports


def write_exports(exports: dict[str, dict[str, Any]], out: Path) -> list[Path]:
    written = []
    for trace_id, files in exports.items():
        folder = Path(out) / trace_id
        folder.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (folder / name).write_text(json.dumps(body, indent=1, sort_keys=True) + "\n",
                                       encoding="utf-8", newline="\n")  # fmt: skip
            written.append(folder / name)
    return written


def seeded_ids(seed: int) -> Any:
    """An OpenTelemetry id generator drawn from one seed: the same run exports the same ids."""
    from opentelemetry.sdk.trace.id_generator import IdGenerator

    class SeededIds(IdGenerator):
        def __init__(self) -> None:
            self._rng = random.Random(f"kintsugi-onboard-export:{seed}")

        def generate_trace_id(self) -> int:
            return self._rng.getrandbits(128) or 1

        def generate_span_id(self) -> int:
            return self._rng.getrandbits(64) or 1

        def is_trace_id_random(self) -> bool:
            return False  # seeded, so W3C's "random trace id" flag must not be claimed

    return SeededIds()


def capture(package: str, tasks: Sequence[dict[str, Any]], *, split: str, seed: int,
            phase: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:  # fmt: skip
    """Run ``tasks`` on the host package in this process; its rows and its exported traces."""
    import importlib

    import neatlogs
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    provider = TracerProvider(id_generator=seeded_ids(seed))
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tags = [f"host:{package}", f"split:{split}", f"seed:{seed}", f"phase:{phase}",
            f"run:export-s{seed}"]  # fmt: skip
    neatlogs.init(api_key=None, workflow_name=f"host:{package}", instrumentations=["anthropic"],
                  tags=tags, disable_export=True, tracer_provider=provider)  # fmt: skip
    try:
        from hostkit.evalrun import run_one

        agent = importlib.import_module(f"{package}.agent").run_task
        grader = importlib.import_module(f"{package}.graders.grader").grade
        rows = [run_one(agent, grader, task, split, seed) for task in tasks]  # in order: ids
        neatlogs.flush()
    finally:
        neatlogs.shutdown()
    return rows, to_exports(memory.get_finished_spans())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a host's tasks and export their traces.")
    parser.add_argument("--host", required=True, help="the host's package")
    parser.add_argument("--split", default="train")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--phase", default="rehearsal")
    parser.add_argument("--out", type=Path, required=True, help="the export directory")
    args = parser.parse_args(argv)
    tasks = [json.loads(line) for line in sys.stdin if line.strip()]
    with LoopbackGuard() as guard:
        rows, exports = capture(args.host, tasks, split=args.split, seed=args.seed,
                                phase=args.phase)  # fmt: skip
    write_exports(exports, args.out)
    for row in rows:
        sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.write(json.dumps({"guard": {"non_loopback_refused": guard.refused},
                                 "traces": len(exports)}) + "\n")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
