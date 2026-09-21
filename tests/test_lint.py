"""The lint: five deterministic checks plus L10 in code, the rest through JEV."""

from __future__ import annotations

import json

from jev.benchmarks.plans import V20_BAD_PLAN, V21_PLAN, V26_PLAN
from jev.client import RecordedJevClient
from jev.contract import Plan
from jev.lint import code_checks, jev_checks, lint_plan


def checks_by_id(results):
    out = {}
    for c in results:
        out.setdefault(c.id, []).append(c)
    return out


def test_reference_plans_pass_every_code_check():
    for plan in (V21_PLAN, V26_PLAN):
        failures = [c.id for c in code_checks(plan) if not c.passed]
        assert failures == []


def test_l1_rejects_a_residual_prompt_on_a_core_plan():
    plan = Plan.parse({
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "",
        "asks": ["Decide something."],
        "states": {"S1": {"source": "user_input", "content": "{{x}}"}},
        "rounds": [{"round": 1, "state": "S1", "questions": [
            {"id": "q1", "type": "noul", "role": "core", "instructions": "Is it so?",
             "criteria": None}]}],
        "policy": [{"when": "any.confidence < 0.5", "then": "escalate"}],
        "residual_prompt": "something", "post_checks": [],
    })
    l1 = checks_by_id(code_checks(plan))["L1"][0]
    assert not l1.passed and "null when jev_role is core" in l1.detail


def test_l1_rejects_verification_inside_a_round():
    plan = Plan.parse({
        "task_shape": "generative", "jev_role": "post", "gate_rationale": "",
        "asks": ["Write it."],
        "states": {"S1": {"source": "llm_output", "content": "{{out}}"}},
        "rounds": [{"round": 1, "state": "S1", "questions": [
            {"id": "v1", "type": "noul", "role": "post",
             "instructions": "Does the output address the brief?", "criteria": None}]}],
        "policy": [], "residual_prompt": "Write it.",
        "post_checks": [{"id": "v1", "type": "noul", "instructions": "Does it?"}],
    })
    l1 = checks_by_id(code_checks(plan))["L1"][0]
    assert not l1.passed and "only in post_checks" in l1.detail


def test_l2_flags_a_threshold_baked_into_a_question():
    plan = _plan_with_question({
        "id": "q1", "type": "noul", "role": "core",
        "instructions": "Is the customer more than 30 days late?", "criteria": None,
    })
    l2 = checks_by_id(code_checks(plan))["L2"][0]
    assert not l2.passed and "q1" in l2.detail


def test_l2_ignores_placeholders():
    plan = _plan_with_question({
        "id": "q1", "type": "noul", "role": "core",
        "instructions": "Does '{{item2}}' mention a refund?", "criteria": None,
    })
    assert checks_by_id(code_checks(plan))["L2"][0].passed


def test_l3_requires_what_and_examples_on_every_level():
    plan = _plan_with_question({
        "id": "q1", "type": "score", "role": "core", "instructions": "How severe is it?",
        "criteria": [{"what": "Low", "examples": ["a typo"]}, {"what": "High"}],
    })
    l3 = checks_by_id(code_checks(plan))["L3"][0]
    assert not l3.passed and "no examples" in l3.detail


def test_l3_accepts_a_string_example_list_but_l3_still_needs_the_list_form():
    # A level written as a bare string keeps its text but loses its examples.
    plan = _plan_with_question({
        "id": "q1", "type": "score", "role": "core", "instructions": "How severe is it?",
        "criteria": ["Low", "High"],
    })
    assert not checks_by_id(code_checks(plan))["L3"][0].passed


def test_l4_wants_the_confidence_rule_when_there_are_questions():
    plan = _plan_with_question(
        {"id": "q1", "type": "noul", "role": "core", "instructions": "Is it so?",
         "criteria": None},
        policy=[{"when": "q1 >= 0.5", "then": "do the thing"}],
    )
    assert not checks_by_id(code_checks(plan))["L4"][0].passed


def test_l4_accepts_a_post_only_plan_that_escalates_after_a_retry():
    assert checks_by_id(code_checks(V21_PLAN))["L4"][0].passed


def test_l5_is_settled_in_code_when_the_residual_prompt_is_verbatim():
    l5 = checks_by_id(code_checks(V21_PLAN))["L5"][0]
    assert l5.passed and l5.where == "code"


def test_l5_goes_to_jev_when_the_residual_prompt_was_rewritten():
    assert "L5" not in checks_by_id(code_checks(V26_PLAN))


def test_l10_catches_criteria_from_on_criteria_the_request_names():
    l10 = checks_by_id(code_checks(V20_BAD_PLAN))["L10"][0]
    assert not l10.passed and "q1" in l10.detail


def test_l10_leaves_criteria_that_only_exist_at_runtime_alone():
    plan = _plan_with_question(
        {"id": "q1", "type": "noul", "role": "pre", "criteria_from": "{{requirements}}",
         "instructions": "Does the CV satisfy 'criterion'?", "criteria": None},
        request="Valuta questo CV rispetto ai requisiti della posizione.",
    )
    assert checks_by_id(code_checks(plan))["L10"][0].passed


# -- JEV side --------------------------------------------------------------


def test_jev_checks_use_one_call_per_state_and_grade_by_polarity():
    client = RecordedJevClient({}, default_noul=0.9)
    results = checks_by_id(jev_checks(V26_PLAN, client))
    # "yes" is good for L5-L8 and L13, and is the defect for L9, L11 and L12.
    assert all(c.passed for c in results["L8"])
    assert all(not c.passed for c in results["L9"])
    assert all(not c.passed for c in results["L12"])
    # The invariant is one call per distinct state, never one call per question.
    states = {json.dumps(state, sort_keys=True, default=str) for state, _ in client.calls}
    assert len(client.calls) == len(states)


def test_l6_runs_one_question_per_ask_on_post_roles():
    results = checks_by_id(jev_checks(V21_PLAN, RecordedJevClient({}, default_noul=0.9)))
    assert len(results["L6"]) == len(V21_PLAN.asks)
    assert [c.target for c in results["L6"]] == ["asks[0]", "asks[1]", "asks[2]"]


def test_l13_runs_one_question_per_plan_question():
    results = checks_by_id(jev_checks(V26_PLAN, RecordedJevClient({}, default_noul=0.9)))
    assert {c.target for c in results["L13"]} == {"q2", "q3"}


def test_a_failing_jev_check_is_advisory_and_a_failing_code_check_blocks():
    report = lint_plan(V20_BAD_PLAN, RecordedJevClient({}, default_noul=0.9))
    assert report.blocked
    assert [c.id for c in report.blocking_failures] == ["L10"]
    assert all(c.where == "jev" for c in report.advisory_failures)


def test_feedback_is_what_the_compiler_retry_receives():
    report = lint_plan(V20_BAD_PLAN)
    assert any(line.startswith("L10") for line in report.feedback())


def _plan_with_question(question, policy=None, request="Fai la cosa."):
    return Plan.parse({
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "",
        "asks": ["Do the thing."],
        "states": {"S1": {"source": "user_input", "content": "{{x}}"}},
        "rounds": [{"round": 1, "state": "S1", "questions": [question]}],
        "policy": policy or [{"when": "any.confidence < 0.5", "then": "escalate"}],
        "residual_prompt": None, "post_checks": [],
    }, request=request)
