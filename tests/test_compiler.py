"""The compiler stage."""

from __future__ import annotations

import json

import pytest

from jev.benchmarks.plans import V26_PLAN
from jev.client import ScriptedLLMClient
from jev.compiler import CompilationError, build_user_message, compile_plan, compiler_system_prompt
from jev.contract import JevRole, TaskShape
from jev.gate import GateDecision


def gate(shape="mixed", role="pre+post"):
    return GateDecision(TaskShape(shape), JevRole(role), "because")


def test_the_prompt_is_v0_4_and_names_the_two_runtime_constructs():
    prompt = compiler_system_prompt()
    assert "for_each" in prompt and "criteria_from" in prompt
    assert "You never answer the user's request" in prompt


def test_a_none_role_never_calls_the_model():
    llm = ScriptedLLMClient([])
    plan = compile_plan(llm, "Traduci questo paragrafo.", gate("generative", "none"))
    assert plan.residual_prompt == "Traduci questo paragrafo."
    assert llm.prompts == []


def test_the_user_message_carries_the_request_and_the_gate_verdict():
    message = build_user_message("Fai la cosa.", gate())
    assert 'Request: "Fai la cosa."' in message
    assert "Gate: mixed, pre+post." in message


def test_retry_feedback_lists_the_failed_checks():
    message = build_user_message("Fai la cosa.", gate(), ["L8 residual prompt adds tone"])
    assert "L8 residual prompt adds tone" in message


def test_the_gate_verdict_wins_over_a_downgrade_by_the_compiler():
    # The economy tier downgraded V10 to none "because the logs are not
    # provided": that is confusing compilation with execution.
    downgraded = dict(V26_PLAN.to_dict(), jev_role="none", task_shape="generative")
    llm = ScriptedLLMClient([json.dumps(downgraded)])
    plan = compile_plan(llm, V26_PLAN.request, gate())
    assert plan.jev_role is JevRole.PRE_POST and plan.task_shape is TaskShape.MIXED


def test_a_completion_that_is_not_a_plan_is_reported_with_the_raw_text():
    llm = ScriptedLLMClient(["Sorry, I can't."])
    with pytest.raises(CompilationError) as exc:
        compile_plan(llm, "Fai la cosa.", gate())
    assert exc.value.raw == "Sorry, I can't."
