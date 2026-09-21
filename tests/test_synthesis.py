"""The synthesis: the decisions, the invented rules and the disagreements."""

from __future__ import annotations

from jev.benchmarks.plans import V26_PLAN
from jev.client import CallableJevClient, RecordedJevClient
from jev.contract import Plan, PolicyRule
from jev.policy import PolicyOutcome, TriggeredRule
from jev.runtime import run_rounds
from jev.lint import CheckResult, LintReport
from jev.synthesis import (
    IT,
    applied_rules,
    Assessment,
    assessments,
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


def test_a_decision_reaches_jev_as_a_question_about_the_input_with_its_answer():
    # V26: handed "question -> yes" with no input, JEV re-asked the question of
    # the reply and flagged a good retention reply (0.11).
    client = RecordedJevClient({"d0": 0.3})
    found = check_consistency(
        client,
        [Assessment("Does the message threaten to withdraw?", "yes (0.92)")],
        "Gentile cliente, restiamo insieme.",
        source="Se il prezzo non cambia disdiciamo.",
    )
    state, ids = client.calls[0]
    assert ids == ("d0",)
    assert state["input"] == "Se il prezzo non cambia disdiciamo."
    assert state["assessments"]["d0"] == {
        "question": "Does the message threaten to withdraw?", "answer": "yes (0.92)"
    }
    assert found == ["Does the message threaten to withdraw -> yes (0.92) (0.30)"]


def test_assessments_keep_item_keys_and_render_answers_for_jev():
    client = RecordedJevClient({"q2": (1.0, 0.9), "q3": ("price", 0.9)})
    result = run_rounds(client, V26_PLAN, {"customer_message": "forse cambiamo"})
    found = assessments(result, V26_PLAN)
    assert len(found) == 2
    assert "price" in found[1].answer
    assert found[1].question == V26_PLAN.question("q3").instructions
    assert all(a.so is None for a in found)  # no policy given, no consequences


def test_a_fired_rule_travels_with_the_decision_it_depends_on():
    # H07: "late > 30 days -> yes" says nothing about tone; the policy does.
    client = RecordedJevClient({"q2": (3.0, 0.9), "q3": ("price", 0.9)})
    result = run_rounds(client, V26_PLAN, {"customer_message": "disdiciamo"})
    policy = PolicyOutcome(triggered=[
        TriggeredRule(PolicyRule("q2.score >= 3", "use a formal tone")),
        TriggeredRule(PolicyRule("q2.score >= 2", "offer a call")),
        TriggeredRule(PolicyRule("any.confidence < 0.5", "escalate")),
    ])
    by_id = {a.question: a for a in assessments(result, V26_PLAN, policy)}
    q2 = by_id[V26_PLAN.question("q2").instructions]
    q3 = by_id[V26_PLAN.question("q3").instructions]
    assert q2.so == "use a formal tone; offer a call"
    assert q3.so is None  # the rules never mention q3, and escalation is not an action

    seen = []

    def ask(state, questions):
        seen.append((state, questions))
        return RecordedJevClient({"d0": 0.9, "d1": 0.9}).ask(state, questions)

    check_consistency(CallableJevClient(ask), [q2, q3], "Spettabile Cliente", source="x")
    state, asked = seen[0]
    assert state["assessments"]["d0"]["so"] == "use a formal tone; offer a call"
    assert "so" not in state["assessments"]["d1"]
    # Only a decision with consequences gets the question that names `so`; the
    # other keeps the wording validated on V26/V09/C13/C03/V18.
    questions = {q.id: q.instructions for q in asked}
    assert "under `so`" in questions["d0"]
    assert questions["d1"] == (
        "`assessments.d1` is a question that was asked about `input` and the answer it "
        "got. Does `output` take that answer into account when it responds to `input`?"
    )


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
