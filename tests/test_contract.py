"""The plan contract."""

from __future__ import annotations

import pytest

from jev.benchmarks.plans import V26_PLAN
from jev.contract import JevRole, Plan, PlanError, TaskShape, passthrough_plan
from jev.primitives import QuestionType


def test_roundtrip_keeps_every_field():
    again = Plan.parse(V26_PLAN.to_dict(), request=V26_PLAN.request)
    assert again.to_dict() == V26_PLAN.to_dict()


def test_a_code_fence_around_the_json_is_tolerated():
    plan = Plan.parse('```json\n{"task_shape":"extraction","jev_role":"none",'
                      '"gate_rationale":"x"}\n```')
    assert plan.task_shape is TaskShape.EXTRACTION


def test_a_non_json_completion_is_a_plan_error():
    with pytest.raises(PlanError):
        Plan.parse("I cannot help with that.")


def test_an_unknown_role_is_a_plan_error():
    with pytest.raises(PlanError):
        Plan.parse({"task_shape": "judgment", "jev_role": "maybe", "gate_rationale": ""})


def test_options_is_accepted_as_an_alias_of_criteria():
    plan = Plan.parse({
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "",
        "asks": ["a"], "states": {"S1": {"source": "user_input", "content": "{{x}}"}},
        "rounds": [{"round": 1, "state": "S1", "questions": [
            {"id": "q1", "type": "choice", "instructions": "Which one?",
             "options": ["price", "service", "other"]}]}],
        "policy": [], "residual_prompt": None, "post_checks": [],
    })
    assert [o.id for o in plan.question("q1").options] == ["price", "service", "other"]


def test_role_flags():
    assert JevRole.PRE_POST.has_pre and JevRole.PRE_POST.has_post
    assert JevRole.POST.has_post and not JevRole.POST.has_pre
    assert JevRole.CORE.is_core and not JevRole.CORE.has_post


def test_state_of_finds_the_state_a_question_runs_against():
    assert V26_PLAN.state_of("q2").id == "S1"
    assert V26_PLAN.state_of("nope") is None


def test_a_none_role_keeps_the_request_verbatim():
    plan = passthrough_plan("Traduci questo paragrafo.", TaskShape.GENERATIVE, "g4 0.17")
    assert plan.jev_role is JevRole.NONE
    assert plan.residual_prompt == "Traduci questo paragrafo."
    assert plan.rounds == () and plan.post_checks == ()


def test_a_template_question_declares_itself():
    plan = Plan.parse({
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "",
        "asks": ["a"], "states": {"S1": {"source": "user_input", "content": "{{x}}"}},
        "rounds": [{"round": 1, "state": "S1", "questions": [
            {"id": "q1", "type": "noul", "instructions": "Is 'item' late?",
             "for_each": "{{orders}}", "criteria": None}]}],
        "policy": [], "residual_prompt": None, "post_checks": [],
    })
    q = plan.question("q1")
    assert q.is_template and q.type is QuestionType.NOUL


def test_an_escalating_rule_is_recognised_from_its_action():
    assert V26_PLAN.policy[-1].escalates
    assert not V26_PLAN.policy[0].escalates
