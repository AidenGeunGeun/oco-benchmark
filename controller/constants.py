"""Shared benchmark controller constants."""

from __future__ import annotations


# Qwen3.6's model-card evaluator budget uses an 81,920-token generation cap.
# OCO enforces output length through both the materialized model limit and the
# OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX process environment value.
QWEN_OUTPUT_TOKEN_LIMIT = 81920
