"""The policy: thresholds and aggregations live here, never in a question."""

from __future__ import annotations

import pytest

from jev.contract import PolicyRule
from jev.policy import PolicyError, Scope, count_above, evaluate, run_policy
from jev.primitives import choice, noul, score


def scope(**answers):
    return Scope(answers)


def test_bare_reference_reads_the_noul_probability():
    assert evaluate("q1 >= 0.5", scope(q1=noul("q1", 0.8)))
    assert not evaluate("q1 >= 0.5", scope(q1=noul("q1", 0.2)))


def test_score_and_confidence_are_separate_operands():
    s = scope(q2=score("q2", 2.0, 0.9))
    assert evaluate("q2.score >= 2 AND q2.confidence >= 0.5", s)
    assert not evaluate("q2.score >= 3", s)


def test_choice_compares_by_option_id():
    s = scope(q3=choice("q3", "price", 0.9))
    assert evaluate('q3.choice == "price"', s)
    assert evaluate("q3 != 'service'", s)


def test_any_confidence_is_the_minimum_over_answers_that_have_one():
    s = scope(q1=noul("q1", 0.9), q2=score("q2", 1.0, 0.44))
    assert evaluate("any.confidence < 0.5", s)


def test_any_confidence_without_confidences_never_escalates():
    # A plan of Nouls only has no confidence anywhere; the uncertainty band,
    # not this rule, is what flags it.
    assert not evaluate("any.confidence < 0.5", scope(q1=noul("q1", 0.5)))


def test_noul_uncertainty_band():
    assert evaluate("any.uncertain", scope(q1=noul("q1", 0.53)))
    assert not evaluate("any.uncertain", scope(q1=noul("q1", 0.91)))


def test_or_and_parentheses():
    s = scope(q1=noul("q1", 0.9), q2=score("q2", 0.0, 0.9))
    assert evaluate("(q1 >= 0.5 OR q2.score >= 3) AND q2.confidence > 0.5", s)
    assert not evaluate("q1 < 0.5 OR q2.score >= 3", s)


def test_unknown_question_is_an_error_not_a_silent_false():
    with pytest.raises(PolicyError):
        evaluate("q9 >= 0.5", scope(q1=noul("q1", 0.9)))


def test_a_broken_rule_is_reported_and_does_not_stop_the_round():
    outcome = run_policy(
        [PolicyRule("q9 >= 0.5", "do something"),
         PolicyRule("any.confidence < 0.5", "escalate")],
        [scope(q1=noul("q1", 0.9), q2=score("q2", 1.0, 0.2))],
    )
    assert outcome.errors and outcome.escalated


def test_rules_fire_once_per_scope_and_remember_the_item():
    rule = PolicyRule("q1 >= 0.5", "list the order as wrong address")
    scopes = [
        Scope({"q1": noul("q1", 0.9, item_key="o1")}, item_key="o1"),
        Scope({"q1": noul("q1", 0.1, item_key="o2")}, item_key="o2"),
    ]
    outcome = run_policy([rule], scopes)
    assert [t.item_key for t in outcome.triggered] == ["o1"]


def test_counts_are_computed_in_code():
    answers = [noul("q1", 0.9), noul("q1", 0.2), noul("q1", 0.7)]
    assert count_above(answers) == 2
    s = Scope({}, counts={"q1": 2})
    assert evaluate("count.q1 >= 2", s)


def test_an_unanswered_question_never_satisfies_a_rule():
    assert not evaluate("q1 >= 0.5", scope(q1=noul("q1", None)))
