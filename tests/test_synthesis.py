"""The synthesis: the decisions, the invented rules and the disagreements."""

from __future__ import annotations

from jev.benchmarks.plans import V26_PLAN
from jev.client import RecordedJevClient
from jev.contract import Plan, PolicyRule
from jev.policy import PolicyOutcome, TriggeredRule
from jev.runtime import run_rounds
from jev.lint import CheckResult, LintReport
from jev.synthesis import (
    IT,
    applied_rules,
    check_consistency,
    describe_decisions,
    implicit_rules,
    synthesize,
)


def test_escalation_is_never_reported_as_an_applied_rule():
    rules = applied_rules(_plan_without_stated_condition())
    assert rules and all("escalate" not in rule for rule, _ in rules)


def test_a_rule_the_request_never_states_asks_for_confirmation():
    # V09: the policy lets the application proceed only if every requirement is
    # met. A reasonable reading of "requisiti", but the user never wrote it.
    assert implicit_rules(_plan_without_stated_condition())


def test_a_request_that_states_its_own_condition_needs_no_confirmation():
    # V26 says "Se sì preparami una risposta di retention": the policy encodes
    # the user's own condition, so it is shown but not questioned.
    rules = applied_rules(V26_PLAN)
    assert rules and not any(needs for _, needs in rules)
    assert implicit_rules(V26_PLAN) == []


def test_a_failed_l12_forces_confirmation_even_on_a_conditional_request():
    lint = LintReport([CheckResult("L12", "no invented decision rule", "jev", False,
                                   score=0.9)])
    assert implicit_rules(V26_PLAN, lint=lint)


def test_the_uncertain_band_is_carried_to_the_user():
    client = RecordedJevClient({"q2": (1.0, 0.44), "q3": ("price", 0.9)})
    result = run_rounds(client, V26_PLAN, {"customer_message": "forse cambiamo"})
    decisions, uncertain = describe_decisions(result, V26_PLAN)
    assert len(decisions) == 2
    assert len(uncertain) == 1 and "q2" not in uncertain[0]


def test_a_disagreement_between_a_decision_and_the_text_is_reported():
    # V18 review 2: JEV called the complaint founded at 0.66, the reply treated
    # it as generic. The synthesis is where that surfaces.
    client = RecordedJevClient({"d0": 0.2, "d1": 0.9})
    found = check_consistency(client, ["review 2 is founded", "review 1 is founded"], "a reply")
    assert found == ["review 2 is founded (0.20)"]


def test_no_decisions_means_no_consistency_call():
    client = RecordedJevClient({})
    assert check_consistency(client, [], "text") == []
    assert client.calls == []


def test_render_puts_the_decision_before_the_generated_text():
    outcome = PolicyOutcome(triggered=[TriggeredRule(PolicyRule("q2.score >= 3", "notify"))])
    synthesis = synthesize(V26_PLAN, None, outcome, output="Gentile cliente...", labels=IT)
    text = synthesis.render()
    assert text.index("Regola applicata") < text.index("[Testo generato]")
    assert "Gentile cliente..." in text


def test_escalation_is_stated_explicitly():
    outcome = PolicyOutcome(triggered=[TriggeredRule(PolicyRule("any.confidence < 0.5",
                                                                "escalate"))])
    assert "Escalation" in synthesize(V26_PLAN, None, outcome).render()


def _plan_without_stated_condition():
    return Plan.parse(
        dict(V26_PLAN.to_dict()),
        request="Questa candidatura va avanti o no? Scrivi anche la mail di esito al candidato.",
    )


def test_a_rule_that_did_not_fire_is_not_reported_as_applied():
    synthesis = synthesize(V26_PLAN, None, PolicyOutcome(triggered=[]), output="testo")
    assert synthesis.applied_rules == []
    assert "Regola applicata" not in synthesis.render()
