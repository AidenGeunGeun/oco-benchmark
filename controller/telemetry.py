"""OCO event-stream parsing and telemetry aggregation."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable


STEP_FIELDS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "cached_prompt_tokens",
    "reasoning_tokens",
    "wall_time_ms",
    "finish_reason",
    "tools_called",
    "step_role",
)

VALID_STEP_ROLES = {"pm", "orchestrator", "auditor", "investigator", "compaction"}

ATTEMPT_AGGREGATION_FIELDS: tuple[str, ...] = (
    "step_count",
    "tool_call_count",
    "tokens_in_total",
    "tokens_out_total",
    "cached_tokens_total",
    "reasoning_tokens_total",
    "prefix_cache_hit_rate",
    "prefix_cache_hit_rate_excluding_first_step",
    "per_step_stats",
)


def load_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def dump_ndjson_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _tool_name(tool: Any) -> str:
    if isinstance(tool, str):
        return tool
    if not isinstance(tool, dict):
        return str(tool)
    name = str(tool.get("name", "unknown"))
    subagent = (
        tool.get("subagent")
        or tool.get("subagent_type")
        or _nested_get(tool, "input", "subagent")
        or _nested_get(tool, "input", "subagent_type")
    )
    if name == "task" and subagent:
        return f"task:{subagent}"
    return name


def _normalize_role(value: Any) -> str:
    role = str(value or "pm").strip().lower()
    aliases = {"primary": "pm", "build": "pm", "plan": "pm"}
    role = aliases.get(role, role)
    return role if role in VALID_STEP_ROLES else "pm"


def _nested_get(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    for key in ("properties", "data", "payload"):
        payload = event.get(key)
        if isinstance(payload, dict):
            return payload
    return event


def _extract_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    for path in (("message", "usage"), ("response", "usage"), ("part", "usage")):
        usage = _nested_get(payload, *path)
        if isinstance(usage, dict):
            return usage
    # Real OCO emits per-step tokens under ``part.tokens`` on step-finish events
    # with input/output/reasoning/cache.{read,write} keys. Coerce that shape to
    # the canonical usage shape so step counting and token aggregation work.
    tokens = _nested_get(payload, "part", "tokens")
    if isinstance(tokens, dict) and any(
        key in tokens for key in ("input", "output", "reasoning", "cache")
    ):
        return _coerce_oco_tokens(tokens)
    return None


def _coerce_oco_tokens(tokens: dict[str, Any]) -> dict[str, Any]:
    cache = tokens.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    usage: dict[str, Any] = {
        "prompt_tokens": int(tokens.get("input") or 0),
        "completion_tokens": int(tokens.get("output") or 0),
    }
    cached_read = int(cache.get("read") or 0)
    if cached_read:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_read}
    reasoning = int(tokens.get("reasoning") or 0)
    if reasoning:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return usage


def _extract_tools(payload: dict[str, Any]) -> list[Any]:
    for key in ("tools_called", "tools", "tool_calls"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate]
    message = payload.get("message")
    if isinstance(message, dict):
        candidate = message.get("tool_calls") or message.get("tools_called")
        if isinstance(candidate, list):
            return [item for item in candidate]
    parts = payload.get("parts") or _nested_get(payload, "message", "parts")
    tools: list[Any] = []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"tool_call", "tool-call"}:
                name = part.get("tool") or part.get("name")
                subagent = _nested_get(part, "input", "subagent_type") or _nested_get(
                    part, "input", "subagent"
                )
                tools.append({"name": name, "subagent": subagent})
    return tools


def _coerce_model_step(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") == "model_step":
        return event
    payload = _event_payload(event)
    usage = _extract_usage(payload)
    if usage is None:
        return None
    role = (
        payload.get("step_role")
        or payload.get("agent")
        or payload.get("role")
        or _nested_get(payload, "message", "agent")
        or _nested_get(payload, "session", "agent")
    )
    finish_reason = (
        payload.get("finish_reason")
        or payload.get("finishReason")
        or _nested_get(payload, "choice", "finish_reason")
        or _nested_get(payload, "part", "reason")
        or "stop"
    )
    wall_time_ms = (
        payload.get("wall_time_ms")
        or payload.get("wallTimeMs")
        or payload.get("duration_ms")
        or payload.get("durationMs")
        or 0
    )
    return {
        "type": "model_step",
        "step_role": role,
        "usage": usage,
        "wall_time_ms": wall_time_ms,
        "finish_reason": finish_reason,
        "tools_called": _extract_tools(payload),
    }


def _rate(numerator: int | float, denominator: int | float) -> float:
    if denominator == 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _median(values: list[int | float]) -> int | float:
    if not values:
        return 0
    value = statistics.median(values)
    return int(value) if float(value).is_integer() else value


def _p95(values: list[int | float]) -> int | float:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    value = ordered[index]
    return int(value) if isinstance(value, float) and value.is_integer() else value


def distribution_stats(values: Iterable[int | float]) -> dict[str, int | float]:
    collected = list(values)
    return {"median": _median(collected), "p95": _p95(collected)}


def _guard_message(message: str) -> dict[str, str] | None:
    if "glob search timed out after" in message:
        return {"tool": "glob", "message": message}
    if "grep search timed out after" in message:
        return {"tool": "grep", "message": message}
    return None


def _extract_oco_tool_use(event: dict[str, Any]) -> dict[str, Any] | None:
    """Pull a single tool record out of an OCO `tool_use` event.

    Real OCO emits each tool call as its own event with shape:
    ``{type: "tool_use", part: {tool, state.input, ...}}``. We extract the
    tool name and any subagent_type so a stateful pass over the event stream
    can attribute it to the containing step.
    """
    if event.get("type") not in {"tool_use", "tool-use"}:
        return None
    part = event.get("part")
    if not isinstance(part, dict):
        return None
    name = part.get("tool") or part.get("name")
    if not name:
        return None
    state_input = _nested_get(part, "state", "input")
    direct_input = part.get("input") if isinstance(part.get("input"), dict) else None
    candidate_input = state_input if isinstance(state_input, dict) else direct_input
    subagent = None
    if isinstance(candidate_input, dict):
        subagent = candidate_input.get("subagent_type") or candidate_input.get(
            "subagent"
        )
    return {"name": name, "subagent": subagent}


def normalize_events(
    events: Iterable[dict[str, Any]], *, attempt_id: str, run_id: str
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    guard_messages: list[dict[str, str]] = []
    patch_diff = ""
    pending_tools: list[dict[str, Any]] = []

    for event in events:
        message = str(event.get("message", ""))
        guard = _guard_message(message)
        if guard is not None:
            guard_messages.append(guard)

        event_type = event.get("type")
        if event_type in {"fixture_diff", "patch_diff"}:
            patch_diff = str(event.get("diff", ""))
            continue

        # Reset per-step tool accumulator at each step-start.
        if event_type in {"step_start", "step-start"}:
            pending_tools = []
            continue

        # Accumulate tool calls until the next step_finish.
        oco_tool = _extract_oco_tool_use(event)
        if oco_tool is not None:
            pending_tools.append(oco_tool)
            continue

        event = _coerce_model_step(event) or {}
        if event.get("type") != "model_step":
            continue

        usage = event.get("usage", {})
        prompt_details = usage.get("prompt_tokens_details", {}) or {}
        completion_details = usage.get("completion_tokens_details", {}) or {}
        event_tools = event.get("tools_called", [])
        if not event_tools and pending_tools:
            event_tools = list(pending_tools)
        tools_called = [_tool_name(tool) for tool in event_tools]
        pending_tools = []
        step = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "cached_prompt_tokens": int(prompt_details.get("cached_tokens", 0)),
            "reasoning_tokens": int(completion_details.get("reasoning_tokens", 0)),
            "wall_time_ms": int(event.get("wall_time_ms", 0)),
            "finish_reason": str(event.get("finish_reason", "stop")),
            "tools_called": tools_called,
            "step_role": _normalize_role(event.get("step_role", "pm")),
        }
        steps.append(step)

    tokens_in_total = sum(step["prompt_tokens"] for step in steps)
    tokens_out_total = sum(step["completion_tokens"] for step in steps)
    cached_tokens_total = sum(step["cached_prompt_tokens"] for step in steps)
    reasoning_tokens_total = sum(step["reasoning_tokens"] for step in steps)
    later_steps = steps[1:]
    later_prompt_tokens = sum(step["prompt_tokens"] for step in later_steps)
    later_cached_tokens = sum(step["cached_prompt_tokens"] for step in later_steps)

    delegation_observed = any(
        step["step_role"] == "pm" and "task:orchestrator" in step["tools_called"]
        for step in steps
    )
    audit_observed = any("task:auditor" in step["tools_called"] for step in steps)
    compaction_events = sum(1 for step in steps if step["step_role"] == "compaction")

    normalized = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "eval_submission_id": f"{run_id}:{attempt_id}",
        "steps": steps,
        "step_count": len(steps),
        "tool_call_count": sum(len(step["tools_called"]) for step in steps),
        "tokens_in_total": tokens_in_total,
        "tokens_out_total": tokens_out_total,
        "cached_tokens_total": cached_tokens_total,
        "reasoning_tokens_total": reasoning_tokens_total,
        "prefix_cache_hit_rate": _rate(cached_tokens_total, tokens_in_total),
        "prefix_cache_hit_rate_excluding_first_step": _rate(
            later_cached_tokens, later_prompt_tokens
        ),
        "wall_time_ms_total": sum(step["wall_time_ms"] for step in steps),
        "per_step_stats": {
            "prompt_tokens": distribution_stats(
                step["prompt_tokens"] for step in steps
            ),
            "completion_tokens": distribution_stats(
                step["completion_tokens"] for step in steps
            ),
            "wall_time_ms": distribution_stats(step["wall_time_ms"] for step in steps),
        },
        "guard_messages": guard_messages,
        "delegation_observed": delegation_observed,
        "audit_observed": audit_observed,
        "full_loop_observed": delegation_observed and audit_observed,
        "compaction_events": compaction_events,
        "patch_diff": patch_diff,
    }
    return normalized


def aggregate_run(attempts: Iterable[dict[str, Any]], *, run_id: str) -> dict[str, Any]:
    collected = list(attempts)
    tokens_in_total = sum(int(item.get("tokens_in_total", 0)) for item in collected)
    cached_tokens_total = sum(
        int(item.get("cached_tokens_total", 0)) for item in collected
    )
    later_in_total = 0
    later_cached_total = 0
    for item in collected:
        for step in item.get("steps", [])[1:]:
            later_in_total += int(step.get("prompt_tokens", 0))
            later_cached_total += int(step.get("cached_prompt_tokens", 0))

    return {
        "run_id": run_id,
        "attempt_count": len(collected),
        "attempt_ids": [str(item.get("attempt_id", "")) for item in collected],
        "step_count": sum(int(item.get("step_count", 0)) for item in collected),
        "tool_call_count": sum(
            int(item.get("tool_call_count", 0)) for item in collected
        ),
        "tokens_in_total": tokens_in_total,
        "tokens_out_total": sum(
            int(item.get("tokens_out_total", 0)) for item in collected
        ),
        "cached_tokens_total": cached_tokens_total,
        "reasoning_tokens_total": sum(
            int(item.get("reasoning_tokens_total", 0)) for item in collected
        ),
        "prefix_cache_hit_rate": _rate(cached_tokens_total, tokens_in_total),
        "prefix_cache_hit_rate_excluding_first_step": _rate(
            later_cached_total, later_in_total
        ),
        "wall_time_ms_total": sum(
            int(item.get("wall_time_ms_total", 0)) for item in collected
        ),
        "attempt_distribution_stats": {
            "step_count": distribution_stats(
                int(item.get("step_count", 0)) for item in collected
            ),
            "tokens_in_total": distribution_stats(
                int(item.get("tokens_in_total", 0)) for item in collected
            ),
            "tokens_out_total": distribution_stats(
                int(item.get("tokens_out_total", 0)) for item in collected
            ),
            "wall_time_ms_total": distribution_stats(
                int(item.get("wall_time_ms_total", 0)) for item in collected
            ),
        },
    }
