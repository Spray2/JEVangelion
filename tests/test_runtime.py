"""The runtime: binding placeholders, expanding templates, one call per state."""

from __future__ import annotations

import pytest

from jev.benchmarks.plans import V26_PLAN
from jev.client import RecordedJevClient
from jev.contract import Plan
from jev.runtime import (
    MissingInput,
    fill_residual_prompt,
    placeholder_name,
    resolve,
    resolved_facts,
    run_rounds,
)


def test_a_state_is_a_placeholder_until_the_input_arrives():
    assert placeholder_name("{{orders}}") == "orders"
    assert placeholder_name("the orders") is None
    assert resolve({"orders": "{{orders}}"}, {"orders": [1, 2]}) == {"orders": [1, 2]}


def test_a_missing_input_is_named():
    with pytest.raises(MissingInput):
        resolve("{{orders}}", {})


def test_for_each_makes_one_state_per_element_so_questions_stay_atomic():
    plan = _for_each_plan()
    client = RecordedJevClient({"q1": 0.9})
    result = run_rounds(client, plan, {"reviews": ["first", "second", "third"]})
    assert result.calls == 3
    assert result.item_keys == ["first", "second", "third"]
    assert [state for state, _ in client.calls] == [
        {"item": "first"}, {"item": "second"}, {"item": "third"}
    ]


def test_criteria_from_instantiates_one_question_per_criterion():
    plan = _criteria_from_plan()
    client = RecordedJevClient({"q1": 0.8})
    result = run_rounds(client, plan, {"cv": "a cv", "requirements": ["degree", "english"]})
    assert result.item_keys == ["degree", "english"]
    states = [state for state, _ in client.calls]
    assert states[0]["criterion"] == "degree" and states[0]["cv"] == "a cv"


def test_scalar_questions_of_one_state_cost_a_single_call():
    client = RecordedJevClient({"q2": (2.0, 0.9), "q3": ("price", 0.9)})
    result = run_rounds(client, V26_PLAN, {"customer_message": "we will cancel"})
    assert result.calls == 1
    assert client.calls[0][1] == ("q2", "q3")


def test_counts_come_from_code_not_from_jev():
    plan = _for_each_plan()
    client = RecordedJevClient({"q1#a": 0.9, "q1#b": 0.2, "q1#c": 0.7})
    result = run_rounds(client, plan, {"reviews": ["a", "b", "c"]})
    assert result.counts() == {"q1": 2}


def test_item_scopes_carry_the_scalar_answers_too():
    plan = _mixed_scalar_and_template_plan()
    client = RecordedJevClient({"q0": 0.9, "q1#a": 0.9, "q1#b": 0.1})
    result = run_rounds(client, plan, {"doc": "d", "reviews": ["a", "b"]})
    scopes = result.scopes()
    assert scopes[0].item_key is None
    assert {s.item_key for s in scopes[1:]} == {"a", "b"}
    assert scopes[1].resolve("q0") == 0.9


def test_the_uncertainty_band_is_reported_not_escalated_by_confidence():
    plan = _for_each_plan()
    result = run_rounds(RecordedJevClient({"q1#a": 0.53, "q1#b": 0.98}), plan,
                        {"reviews": ["a", "b"]})
    assert [a.item_key for a in result.uncertain] == ["a"]


def test_resolved_outcomes_reach_the_prompt_as_facts():
    client = RecordedJevClient({"q2": (3.0, 0.9), "q3": ("price", 0.9)})
    result = run_rounds(client, V26_PLAN, {"customer_message": "we cancel"})
    facts = resolved_facts(result, V26_PLAN)
    filled = fill_residual_prompt(V26_PLAN, facts)
    assert "{{q2}}" not in filled and "{{q3}}" not in filled
    assert "price" in filled
    assert "Formal notice of cancellation" in filled


def test_rounds_run_in_order():
    plan = Plan.parse({
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "",
        "asks": ["a"], "states": {"S1": {"source": "user_input", "content": "{{x}}"}},
        "rounds": [
            {"round": 2, "state": "S1", "questions": [
                {"id": "q2", "type": "noul", "role": "core", "instructions": "Second?",
                 "criteria": None}]},
            {"round": 1, "state": "S1", "questions": [
                {"id": "q1", "type": "noul", "role": "core", "instructions": "First?",
                 "criteria": None}]},
        ],
        "policy": [], "residual_prompt": None, "post_checks": [],
    })
    client = RecordedJevClient({})
    run_rounds(client, plan, {"x": "value"})
    assert [ids for _, ids in client.calls] == [("q1",), ("q2",)]


def _for_each_plan():
    return Plan.parse({
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "",
        "asks": ["For each review, decide whether it is founded."],
        "states": {"S1": {"source": "user_input", "content": {"reviews": "{{reviews}}"}}},
        "rounds": [{"round": 1, "state": "S1", "questions": [
            {"id": "q1", "type": "noul", "role": "core", "for_each": "{{reviews}}",
             "instructions": "Does 'item' describe a specific, verifiable event?",
             "criteria": None}]}],
        "policy": [{"when": "any.confidence < 0.5", "then": "escalate that review"}],
        "residual_prompt": None, "post_checks": [],
    })


def _criteria_from_plan():
    return Plan.parse({
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "",
        "asks": ["For each requirement, decide whether the CV satisfies it."],
        "states": {"S1": {"source": "user_input",
                          "content": {"cv": "{{cv}}", "requirements": "{{requirements}}"}}},
        "rounds": [{"round": 1, "state": "S1", "questions": [
            {"id": "q1", "type": "noul", "role": "pre", "criteria_from": "{{requirements}}",
             "instructions": "Does the CV satisfy 'criterion'?", "criteria": None}]}],
        "policy": [{"when": "any.confidence < 0.5", "then": "escalate"}],
        "residual_prompt": None, "post_checks": [],
    })


def _mixed_scalar_and_template_plan():
    return Plan.parse({
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "",
        "asks": ["a"],
        "states": {"S1": {"source": "user_input",
                          "content": {"doc": "{{doc}}", "reviews": "{{reviews}}"}}},
        "rounds": [{"round": 1, "state": "S1", "questions": [
            {"id": "q0", "type": "noul", "role": "core", "instructions": "Is the doc signed?",
             "criteria": None},
            {"id": "q1", "type": "noul", "role": "core", "for_each": "{{reviews}}",
             "instructions": "Is 'item' negative?", "criteria": None}]}],
        "policy": [], "residual_prompt": None, "post_checks": [],
    })
