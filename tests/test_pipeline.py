"""End to end: gate, compiler, lint, rounds, policy, generation, checks, synthesis."""

from __future__ import annotations

import json

from jev.benchmarks.cases import by_id
from jev.benchmarks.plans import V21_PLAN, V26_PLAN
from jev.client import CallableJevClient, RecordedJevClient, ScriptedLLMClient
from jev.contract import JevRole
from jev.pipeline import run
from jev.primitives import Answer, QuestionType


class StagedJevClient:
    """One client for the whole run: the gate answers, then everything else."""

    model_version = "jev-1.13.0"

    #: A clean lint, so a test can fail post checks without also failing the
    #: lint and spending the compiler's retry.
    CLEAN_LINT = {
        "L5": 0.9, "L6": 0.9, "L7": 0.9, "L8": 0.9, "L13": 0.9,
        "L9": 0.05, "L11": 0.05, "L12": 0.05,
    }

    def __init__(self, gate_answers, defaults=0.9, overrides=None):
        self.gate = RecordedJevClient(gate_answers)
        self.rest = RecordedJevClient({**self.CLEAN_LINT, **(overrides or {})},
                                      default_noul=defaults)
        self.used_gate = False

    def ask(self, state, questions):
        if not self.used_gate:
            self.used_gate = True
            return self.gate.ask(state, questions)
        return self.rest.ask(state, questions)


def test_a_none_role_goes_straight_to_the_llm_and_never_compiles():
    case = by_id("V05")
    jev = StagedJevClient(case.recorded_answers())
    llm = ScriptedLLMClient(["OAuth2 funziona così..."])
    result = run(case.request, jev=jev, llm=llm)
    assert result.gate.jev_role is JevRole.NONE
    assert result.plan is None and result.lint is None
    assert result.generated.startswith("OAuth2")
    assert llm.prompts[0][1] == case.request


def test_a_none_role_still_hands_the_inputs_to_the_llm():
    # Semiconductors in Chat: gate judgment/none, and the LLM answered without
    # the articles it was meant to judge.
    case = by_id("V05")
    llm = ScriptedLLMClient(["6/10"])
    run(case.request, jev=StagedJevClient(case.recorded_answers()), llm=llm,
        inputs={"articles": [{"id": "a1", "text": "SOX +3%"}], "note": "oggi"})
    prompt = llm.prompts[0][1]
    assert prompt.startswith(case.request + "\n\n")
    assert '"text": "SOX +3%"' in prompt and "note:\noggi" in prompt


def test_an_escalating_gate_stops_before_the_compiler():
    case = by_id("H07")
    result = run(case.request, jev=StagedJevClient(case.recorded_answers()),
                 llm=ScriptedLLMClient([]))
    assert result.escalated and result.plan is None
    # An escalation is an answer too: it says why and what to clarify.
    assert "la richiesta è ambigua" in result.text
    assert "confidence 0.36 sotto la soglia 0.5" in result.text


def test_an_escalation_on_an_english_request_is_explained_in_english():
    jev = StagedJevClient({"shape": ("judgment", 0.29)})
    result = run("Rate how likely chip stocks are to rise today.", jev=jev,
                 llm=ScriptedLLMClient([]))
    assert result.escalated
    assert result.text.startswith("Escalation: the request is ambiguous")
    assert "most likely shape: judgment" in result.text


def test_a_core_plan_produces_decisions_and_no_text():
    jev = StagedJevClient(
        {"shape": ("judgment", 0.99), "g3": 0.95},
        overrides={"q1#o1": 0.9, "q1#o2": 0.1},
    )
    llm = ScriptedLLMClient([json.dumps(_core_plan_dict())])
    result = run("Dimmi quali di questi ordini sono stati spediti all'indirizzo sbagliato.",
                 jev=jev, llm=llm, inputs={"orders": [{"id": "o1"}, {"id": "o2"}]})
    assert result.plan.jev_role is JevRole.CORE
    assert result.generated == ""
    assert len(llm.prompts) == 1  # the compiler only; nothing was generated
    assert result.policy.for_item("o1") and not result.policy.for_item("o2")
    assert "o1" in result.synthesis.render()


def test_a_pre_post_run_injects_the_decisions_and_verifies_the_output():
    jev = StagedJevClient(
        {"shape": ("mixed", 1.0)},
        overrides={"q2": (3.0, 0.9), "q3": ("price", 0.9)},
    )
    llm = ScriptedLLMClient([V26_PLAN.to_json(), "Gentile cliente, le proponiamo uno sconto."])
    result = run(V26_PLAN.request, jev=jev, llm=llm,
                 inputs={"customer_message": "disdiciamo il contratto"})

    generation_prompt = llm.prompts[1][1]
    assert "{{q2}}" not in generation_prompt and "price" in generation_prompt
    assert result.verification.ok
    assert not result.escalated
    # The user is told the decision, not just handed the text.
    rendered = result.synthesis.render()
    assert "price" in rendered and "Gentile cliente" in rendered


def test_runtime_inputs_left_in_the_residual_prompt_reach_the_generation():
    # Without this the main LLM writes a reply to a message it never saw.
    plan = json.loads(V26_PLAN.to_json())
    plan["residual_prompt"] = "Rispondi a: {{customer_message}}. Esito: {{q3}}."
    jev = StagedJevClient({"shape": ("mixed", 1.0)},
                          overrides={"q2": (3.0, 0.9), "q3": ("price", 0.9)})
    llm = ScriptedLLMClient([json.dumps(plan), "Gentile cliente"])
    run(V26_PLAN.request, jev=jev, llm=llm,
        inputs={"customer_message": "disdiciamo il contratto"})
    assert llm.prompts[1][1] == "Rispondi a: disdiciamo il contratto. Esito: price."


def test_a_failed_post_check_costs_one_regeneration_then_escalates():
    jev = StagedJevClient({"shape": ("generative", 0.99), "g4": 0.9}, defaults=0.1)
    llm = ScriptedLLMClient([V21_PLAN.to_json(), "erste Fassung", "zweite Fassung"])
    result = run(V21_PLAN.request, jev=jev, llm=llm)
    assert result.verification.attempts == 2
    assert result.escalated
    assert "post checks still failing" in " ".join(result.notes)
    assert result.generated == "zweite Fassung"


def test_a_regeneration_that_passes_does_not_escalate():
    jev = StagedJevClient({"shape": ("generative", 0.99), "g4": 0.9})
    # The post checks fail on the first draft and pass on the second.
    plain = jev.rest
    rounds = {"n": 0}

    def ask(state, questions):
        if not any(q.id.startswith("v") for q in questions):
            return plain.ask(state, questions)
        rounds["n"] += 1
        value = 0.1 if rounds["n"] == 1 else 0.9
        return {q.id: Answer(q.id, QuestionType.NOUL, probability=value) for q in questions}

    jev.rest = CallableJevClient(ask)
    llm = ScriptedLLMClient([V21_PLAN.to_json(), "erste Fassung", "zweite Fassung"])
    result = run(V21_PLAN.request, jev=jev, llm=llm)
    assert result.verification.attempts == 2
    assert result.verification.ok and not result.escalated
    assert result.generated == "zweite Fassung"


def test_a_blocking_lint_failure_stops_before_any_jev_round():
    broken = dict(V26_PLAN.to_dict(), policy=[{"when": "q2.score >= 3", "then": "notify"}])
    jev = StagedJevClient({"shape": ("mixed", 1.0)})
    llm = ScriptedLLMClient([json.dumps(broken), json.dumps(broken)])
    result = run(V26_PLAN.request, jev=jev, llm=llm,
                 inputs={"customer_message": "disdiciamo"})
    assert result.blocked and result.rounds is None
    assert any("L4" in note for note in result.notes)
    # The user is told why, not handed an empty answer.
    assert result.text.startswith("Il piano compilato non ha superato") and "L4" in result.text


def test_the_lint_retries_the_compiler_once_and_keeps_the_better_plan():
    broken = dict(V26_PLAN.to_dict(), policy=[{"when": "q2.score >= 3", "then": "notify"}])
    jev = StagedJevClient({"shape": ("mixed", 1.0)},
                          overrides={"q2": (3.0, 0.9), "q3": ("price", 0.9)})
    llm = ScriptedLLMClient([json.dumps(broken), V26_PLAN.to_json(), "una risposta"])
    result = run(V26_PLAN.request, jev=jev, llm=llm,
                 inputs={"customer_message": "disdiciamo"})
    assert result.lint_retried and not result.blocked
    assert len(result.plan.policy) == 3


def _core_plan_dict():
    return {
        "task_shape": "judgment", "jev_role": "core",
        "gate_rationale": "Same judgment over a runtime collection.",
        "asks": ["For each order, decide whether it was shipped to the wrong address."],
        "states": {"S1": {"source": "user_input", "content": {"orders": "{{orders}}"}}},
        "rounds": [{"round": 1, "state": "S1", "questions": [
            {"id": "q1", "type": "noul", "role": "core", "for_each": "{{orders}}",
             "instructions": "Does the shipping address of 'item' differ from the customer "
                             "address on record in 'item'?", "criteria": None}]}],
        "policy": [{"when": "q1 >= 0.5", "then": "list the order as wrong address"},
                   {"when": "any.confidence < 0.5", "then": "escalate that order"}],
        "residual_prompt": None, "post_checks": [],
    }
