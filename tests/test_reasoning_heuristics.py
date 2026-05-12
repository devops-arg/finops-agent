"""Reasoning engine helpers — pure-function heuristics that don't need an LLM."""

from __future__ import annotations

from backend.llm.provider import ChatResponse, LLMProvider
from backend.reasoning.engine import ReasoningEngine
from backend.tools.registry import ToolRegistry


class _StubLLM(LLMProvider):
    """Minimal LLM stub — never actually called by these tests."""

    @property
    def model_name(self) -> str:
        return "stub-model"

    @property
    def provider_name(self) -> str:
        return "stub"

    def format_tool_for_provider(self, tool_def):
        return tool_def

    def chat_completion(self, messages, tools=None, temperature=0.0, max_tokens=4096):
        return ChatResponse()


def _engine() -> ReasoningEngine:
    return ReasoningEngine(_StubLLM(), ToolRegistry())


# ── Plan-vs-action heuristic ─────────────────────────────────────────────────


def test_plan_text_without_data_is_detected_as_plan():
    eng = _engine()
    assert (
        eng._looks_like_plan("I will start by querying Cost Explorer, then I'll analyze the breakdown.")
        is True
    )


def test_text_with_real_numbers_is_not_a_plan():
    eng = _engine()
    assert (
        eng._looks_like_plan("The cost increased by $1,243 this week — top driver is RDS at $4,500.") is False
    )


def test_pure_data_response_is_not_a_plan():
    eng = _engine()
    assert eng._looks_like_plan("Total spend: $12,345.67") is False


# ── Param normalization ─────────────────────────────────────────────────────


def test_normalize_unwraps_properties_envelope():
    """Some LLMs wrap params in {"properties": {...}} — strip it."""
    eng = _engine()
    out = eng._normalize_params({"properties": {"command": "aws ec2 describe-instances"}})
    assert out == {"command": "aws ec2 describe-instances"}


def test_normalize_unwraps_parameters_envelope():
    eng = _engine()
    out = eng._normalize_params({"parameters": {"x": 1}})
    assert out == {"x": 1}


def test_normalize_strips_trailing_colon_from_keys():
    eng = _engine()
    out = eng._normalize_params({"region:": "us-east-1", "command": "aws s3 ls"})
    assert "region" in out
    assert "region:" not in out
    assert out["region"] == "us-east-1"


def test_normalize_passthrough_when_clean():
    eng = _engine()
    out = eng._normalize_params({"command": "aws rds describe-db-instances", "max_results": 50})
    assert out == {"command": "aws rds describe-db-instances", "max_results": 50}


# ── Truncation ──────────────────────────────────────────────────────────────


def test_truncate_short_string_unchanged():
    eng = _engine()
    assert eng._truncate("hello", 100) == "hello"


def test_truncate_long_string_marks_total_length():
    eng = _engine()
    big = "x" * 5000
    out = eng._truncate(big, 1000)
    assert out.startswith("x" * 1000)
    assert "5000 total chars" in out
    assert len(out) > 1000  # the marker adds bytes
