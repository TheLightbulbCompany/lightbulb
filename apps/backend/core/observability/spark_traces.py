"""Spark's work log and conversation turns as OpenTelemetry traces (OTLP/JSON), built from the store's rows.

Pure functions, no I/O. `spark_store.log_rows` (and the voice session's end) produce the rows, already scrubbed and
cut; this module only changes their shape. Every span also carries `spark.session_id`, the stable join key whatever
the conventions' names do next. One uploaded task is one trace: the root span is the agent invocation, each
step a child span: `execute_tool` for a tool, `chat {kind}` (the text as an assistant message) for thinking,
commentary and note. A conversation turn is a span in the trace of its task, else of its session.

Read on 2026-11-26 (all "Development" status, so names may still change):
- GenAI tool spans: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/execute-tool.md
  (span `execute_tool {gen_ai.tool.name}`, INTERNAL; gen_ai.tool.call.arguments / .result are Opt-In)
- GenAI agent spans: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md
  (in-process agent: `invoke_agent {gen_ai.agent.name}`, INTERNAL) and client-invoke-agent.md in the same folder
- GenAI realtime spans: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-realtime.md
  (`realtime_inference {model}`; gen_ai.realtime_session.id; conversation.id must not hold a realtime session id)
- Message content: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md
  ("Recording content on attributes": gen_ai.input.messages / gen_ai.output.messages are AnyValue lists of
  {role, parts:[{type:"text", content}]}; a message's finish_reason is deprecated, so the task's outcome goes in
  gen_ai.response.finish_reasons, values from model/gen-ai/gen-ai-output-messages.json) and the registry
  https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/registry/attributes/gen-ai.md
- user.id, os.type, service.name, service.version: https://opentelemetry.io/docs/specs/semconv/registry/attributes/
  (user.md, os.md, service.md); errors: https://opentelemetry.io/docs/specs/semconv/general/recording-errors/
- OTLP/JSON encoding: https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding (hex trace and span ids,
  enums as integers, 64-bit integers, nanosecond timestamps included, as decimal strings, AnyValue wrappers)
- File layout (JSON Lines, `.jsonl`): https://opentelemetry.io/docs/specs/otel/protocol/file-exporter/
"""

from __future__ import annotations

import hashlib

INTERNAL, CLIENT = 1, 3  # SpanKind
UNSET, OK, ERROR = 0, 1, 2  # Status.code
SCOPE = {"name": "lightbulb.spark.backend"}
_SAID = ("thinking", "commentary", "note")  # steps that are the brain's or the engine's words, not a tool call
_OS = {"mac": "darwin", "windows": "windows"}


def _id(nbytes: int, *parts: str) -> str:
    """Deterministic hex id: the same inputs (a retried upload) give the same ids. The person is always an input, so
    another person's untrusted task id cannot land in their trace."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[: nbytes * 2]


def _any(v: object) -> dict:
    """An OTLP/JSON AnyValue."""
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}  # int64 is a decimal string in OTLP/JSON
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [_any(x) for x in v]}}
    if isinstance(v, dict):
        return {"kvlistValue": {"values": [{"key": k, "value": _any(x)} for k, x in v.items()]}}
    return {"stringValue": str(v)}


def _attrs(**kv: object) -> list[dict]:
    """Attributes from keyword pairs (`__` stands for `.`; a None value is left out)."""
    return [{"key": k.replace("__", "."), "value": _any(v)} for k, v in kv.items() if v is not None]


def _message(role: str, text: str) -> list[dict]:
    return [{"role": role, "parts": [{"type": "text", "content": text}]}]


def _span(trace: str, span: str, parent: str | None, name: str, kind: int, start_us: int, ms: int | None) -> dict:
    return {
        "traceId": trace,
        "spanId": span,
        **({"parentSpanId": parent} if parent else {}),
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(start_us * 1000),
        "endTimeUnixNano": str((start_us + (ms or 0) * 1000) * 1000),
    }


def _task_span(r: dict, trace: str) -> dict:
    status = r.get("status")
    s = _span(trace, _id(8, "task", r["user_id"], r["task_id"]), None, "invoke_agent spark", INTERNAL, r["ts"], r["ms"])
    s["attributes"] = _attrs(
        gen_ai__operation__name="invoke_agent",
        gen_ai__agent__name="spark",
        gen_ai__conversation__id=r["session_id"],
        gen_ai__input__messages=_message("user", r["input"]) if r["input"] is not None else None,
        gen_ai__output__messages=_message("assistant", r["result"]) if r["result"] is not None else None,
        gen_ai__response__finish_reasons={"done": ["stop"], "failed": ["error"]}.get(status),  # raw status below
        error__type="task_failed" if status == "failed" else None,
        spark__row_id=r["row_id"],
        spark__session_id=r["session_id"],
        spark__task_id=r["task_id"],
        spark__kind=r["kind"],
        spark__step_no=r["step_no"],
        spark__source=r["source"],
        spark__automation_id=r["automation_id"],
        spark__text_kept=r["text_kept"],
        spark__task__status=status,
        spark__duration_ms=r["ms"],
        spark__first_thinking_ms=r.get("first_thinking_ms"),
        spark__first_commentary_ms=r.get("first_commentary_ms"),
        spark__first_action_ms=r.get("first_action_ms"),
    )
    s["status"] = {"code": {"done": OK, "failed": ERROR}.get(status, UNSET)}
    return s


def _step_span(r: dict, trace: str) -> dict:
    tool = r["tool"] or r["kind"]  # a step with no tool (a note, a screenshot) is named by its kind
    said = r["kind"] in _SAID  # the brain's own words: an assistant message, not a tool call
    s = _span(
        trace,
        _id(8, "step", r["user_id"], r["task_id"], str(r["step_no"])),
        _id(8, "task", r["user_id"], r["task_id"]),
        f"chat {r['kind']}" if said else f"execute_tool {tool}",
        INTERNAL,
        r["ts"],
        r["ms"],
    )
    s["attributes"] = _attrs(
        gen_ai__operation__name="chat" if said else "execute_tool",
        gen_ai__tool__name=None if said else tool,
        gen_ai__conversation__id=r["session_id"],
        gen_ai__output__messages=_message("assistant", r["result"]) if said and r["result"] is not None else None,
        gen_ai__tool__call__arguments=r["input"],
        gen_ai__tool__call__result=None if said else r["result"],
        error__type="tool_failed" if r["ok"] is False else None,
        spark__row_id=r["row_id"],
        spark__session_id=r["session_id"],
        spark__task_id=r["task_id"],
        spark__kind=r["kind"],
        spark__step_no=r["step_no"],
        spark__source=r["source"],
        spark__automation_id=r["automation_id"],
        spark__text_kept=r["text_kept"],
        spark__tool=r["tool"],
        spark__app=r["app"],
        spark__thread=r.get("thread"),
        spark__ok=r["ok"],
        spark__duration_ms=r["ms"],
    )
    s["status"] = {"code": {True: OK, False: ERROR}.get(r["ok"], UNSET)}
    return s


def _turn_span(r: dict, trace: str) -> dict:
    voice = r["source"] == "voice"
    person = r["role"] == "person"
    parent = _id(8, "task", r["user_id"], r["task_id"]) if r.get("task_id") else None
    op = "realtime_inference" if voice else "chat"
    model = r.get("model") if voice else None
    name = f"{op} {model}" if model else op  # `{gen_ai.operation.name} {gen_ai.request.model}`
    s = _span(trace, _id(8, "turn", r["user_id"], r["row_id"]), parent, name, CLIENT, r["ts"], None)
    s["attributes"] = _attrs(
        gen_ai__operation__name=op,
        gen_ai__request__model=model,
        gen_ai__realtime_session__id=r["session_id"] if voice else None,
        gen_ai__conversation__id=None if voice else r["session_id"],
        gen_ai__input__messages=_message("user", r["text"]) if person else None,
        gen_ai__output__messages=None if person else _message("assistant", r["text"]),
        spark__row_id=r["row_id"],
        spark__session_id=r["session_id"],
        spark__task_id=r.get("task_id"),
        spark__role=r["role"],
        spark__source=r["source"],
    )
    s["status"] = {"code": UNSET}
    return s


def _trace_id(r: dict) -> str:
    """One trace per task; a turn with no task shares its session's trace; with neither, its own."""
    if r.get("task_id"):
        return _id(16, "trace", r["user_id"], r["task_id"])
    if r.get("session_id"):
        return _id(16, "session", r["user_id"], r["session_id"])
    return _id(16, "row", r["user_id"], r["row_id"])


def to_otlp(turns: list[dict], steps: list[dict]) -> list[dict]:
    """The rows as OTLP/JSON trace exports, one per trace (so one JSONL line each). `steps` are `log_rows`' task rows
    (step 0 is the task itself); `turns` are its typed lines or a voice session's turns. The person is the rows' own
    `user_id`, which the server set, never the app."""
    grouped: dict[str, dict[tuple, list[dict]]] = {}
    for rows, build in ((turns, _turn_span), (steps, None)):
        for r in rows:
            trace = _trace_id(r)
            span = build(r, trace) if build else (_task_span if r["step_no"] == 0 else _step_span)(r, trace)
            grouped.setdefault(trace, {}).setdefault(
                (r["user_id"], r.get("platform"), r.get("app_version")), []
            ).append(span)
    return [
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": _attrs(
                            service__name="spark",
                            service__version=version,
                            os__type=_OS.get(platform),
                            spark__platform=platform,
                            user__id=user,
                        )
                    },
                    "scopeSpans": [{"scope": SCOPE, "spans": spans}],
                }
                for (user, platform, version), spans in by_resource.items()
            ]
        }
        for by_resource in grouped.values()
    ]
