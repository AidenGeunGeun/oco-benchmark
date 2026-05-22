from __future__ import annotations

from controller.fixtures import fixture_events
from controller.telemetry import (
    ATTEMPT_AGGREGATION_FIELDS,
    STEP_FIELDS,
    aggregate_run,
    normalize_events,
)


def test_normalized_telemetry_shape_and_aggregations() -> None:
    normalized = normalize_events(
        fixture_events("telemetry"), attempt_id="telemetry", run_id="run"
    )

    assert normalized["step_count"] == 3
    assert normalized["tool_call_count"] == 4
    assert all(tuple(step.keys()) == STEP_FIELDS for step in normalized["steps"])
    for field in ATTEMPT_AGGREGATION_FIELDS:
        assert field in normalized
    assert normalized["tokens_in_total"] == 5100
    assert normalized["cached_tokens_total"] == 2950
    assert (
        normalized["prefix_cache_hit_rate_excluding_first_step"]
        > normalized["prefix_cache_hit_rate"]
    )
    assert normalized["per_step_stats"]["wall_time_ms"]["p95"] == 2800


def test_guard_messages_are_recorded() -> None:
    normalized = normalize_events(
        fixture_events("guard", include_guard=True),
        attempt_id="guard",
        run_id="run",
    )

    assert {message["tool"] for message in normalized["guard_messages"]} == {
        "glob",
        "grep",
    }


def test_observation_strata_and_compaction_counter_are_recorded() -> None:
    events = fixture_events("strata")
    events.append(
        {
            "type": "model_step",
            "step_role": "compaction",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 8},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
            "wall_time_ms": 100,
            "finish_reason": "stop",
            "tools_called": [],
        }
    )

    normalized = normalize_events(events, attempt_id="strata", run_id="run")

    assert normalized["delegation_observed"] is True
    assert normalized["audit_observed"] is True
    assert normalized["full_loop_observed"] is True
    assert normalized["compaction_events"] == 1
    assert normalized["steps"][-1]["step_role"] == "compaction"


def test_raw_oco_event_shapes_are_coerced_into_model_steps() -> None:
    raw_events = [
        {
            "type": "message.updated",
            "properties": {
                "agent": "pm",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
                "tool_calls": [
                    {"name": "task", "input": {"subagent_type": "orchestrator"}}
                ],
                "finishReason": "tool_calls",
                "durationMs": 250,
            },
        },
        {"type": "patch_diff", "diff": "diff --git a/a b/a\n"},
    ]

    normalized = normalize_events(raw_events, attempt_id="raw", run_id="run")

    assert normalized["step_count"] == 1
    assert normalized["steps"][0]["tools_called"] == ["task:orchestrator"]
    assert normalized["delegation_observed"] is True
    assert normalized["patch_diff"].startswith("diff --git")


def test_real_oco_step_events_with_part_tokens_are_counted() -> None:
    """OCO emits step_start / tool_use / step_finish events where token usage
    lives at ``part.tokens`` with input/output/reasoning/cache.read keys, not
    at the canonical ``usage.prompt_tokens`` shape. Tool calls arrive as
    separate ``tool_use`` events between step_start and step_finish. The
    telemetry parser must coerce the token shape AND attribute the
    interleaved tool calls to their containing step.
    """
    raw_events = [
        {"type": "step_start", "part": {"type": "step-start"}},
        {
            "type": "tool_use",
            "part": {
                "tool": "write",
                "state": {"input": {"filePath": "x.txt", "content": "hi"}},
                "type": "tool",
            },
        },
        {
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "reason": "tool-calls",
                "tokens": {
                    "input": 1000,
                    "output": 50,
                    "reasoning": 10,
                    "cache": {"read": 200, "write": 0},
                },
            },
        },
        {"type": "step_start", "part": {"type": "step-start"}},
        {
            "type": "tool_use",
            "part": {
                "tool": "task",
                "state": {
                    "input": {
                        "subagent_type": "orchestrator",
                        "description": "delegate work",
                    }
                },
                "type": "tool",
            },
        },
        {
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "reason": "tool-calls",
                "tokens": {
                    "input": 1500,
                    "output": 30,
                    "reasoning": 0,
                    "cache": {"read": 900, "write": 0},
                },
            },
        },
        {
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "reason": "stop",
                "tokens": {
                    "input": 1700,
                    "output": 20,
                    "reasoning": 0,
                    "cache": {"read": 1500, "write": 0},
                },
            },
        },
        {"type": "patch_diff", "diff": "diff --git a/x b/x\n"},
    ]

    normalized = normalize_events(raw_events, attempt_id="real", run_id="run")

    assert normalized["step_count"] == 3
    assert normalized["tokens_in_total"] == 4200
    assert normalized["tokens_out_total"] == 100
    assert normalized["cached_tokens_total"] == 2600
    assert normalized["reasoning_tokens_total"] == 10
    assert normalized["steps"][0]["tools_called"] == ["write"]
    assert normalized["steps"][1]["tools_called"] == ["task:orchestrator"]
    assert normalized["steps"][2]["tools_called"] == []
    assert normalized["steps"][0]["finish_reason"] == "tool-calls"
    assert normalized["steps"][2]["finish_reason"] == "stop"
    assert normalized["delegation_observed"] is True
    assert normalized["patch_diff"].startswith("diff --git")


def test_run_summary_aggregates_attempts() -> None:
    first = normalize_events(fixture_events("one"), attempt_id="one", run_id="run")
    second = normalize_events(fixture_events("two"), attempt_id="two", run_id="run")
    summary = aggregate_run([first, second], run_id="run")

    assert summary["attempt_count"] == 2
    assert summary["step_count"] == first["step_count"] + second["step_count"]
    assert (
        summary["tokens_in_total"]
        == first["tokens_in_total"] + second["tokens_in_total"]
    )
    assert "attempt_distribution_stats" in summary
