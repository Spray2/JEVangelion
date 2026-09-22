"""Scores over a collection: per-item levels on the user's scale, and their mean.

The case that asked for it: "score each article 0-10 on whether chip stocks
rise today, then give the overall score as the mean".
"""

from __future__ import annotations

import json

import pytest

from jev.client import RecordedJevClient, ScriptedLLMClient
from jev.compiler import COMPILER_PROMPT_VERSION, compiler_system_prompt
from jev.contract import Plan, PolicyRule
from jev.pipeline import run
from jev.policy import PolicyError, Scope, run_policy
from jev.primitives import Question
from jev.runtime import run_rounds

REQUEST = ("Valuta ciascuno degli articoli forniti con uno score da 0 a 10 sulla probabilità "
           "che oggi le azioni dei semiconduttori salgano, poi calcola il punteggio "
           "complessivo come media degli score.")
LEVELS = [
    {"what": "Clearly points to a fall", "examples": ["futures sharply down"]},
    {"what": "Leans towards a fall", "examples": ["profit taking expected"]},
    {"what": "Neutral", "examples": ["mixed signals"]},
    {"what": "Leans towards a rise", "examples": ["strong close yesterday"]},
    {"what": "Clearly points to a rise", "examples": ["record highs, broad rally"]},
]


def plan_dict(scale=(0, 10)):
    q = {"id": "q1", "type": "score", "role": "core", "for_each": "{{articles}}",
         "instructions": "How strongly does 'item' point to chip stocks rising today?",
         "criteria": LEVELS}
    if scale is not None:
        q["scale"] = list(scale)
    return {
        "task_shape": "judgment", "jev_role": "core", "gate_rationale": "per-item score",
        "asks": ["Score each article, then average."],
        "states": {"S1": {"source": "user_input", "content": {"articles": "{{articles}}"}}},
        "rounds": [{"round": 1, "state": "S1", "questions": [q]}],
        "policy": [{"when": "mean.q1 >= 2", "then": "report a leaning to rise"},
                   {"when": "any.confidence < 0.5", "then": "escalate"}],
        "residual_prompt": None, "post_checks": [],
    }


ARTICLES = [{"id": "a1", "text": "SOX +3%"}, {"id": "a2", "text": "futures flat"},
            {"id": "a3", "text": "Kospi +2%"}]
ANSWERS = {"q1#a1": (3.4, 0.9), "q1#a2": (1.6, 0.8), "q1#a3": (3.0, 0.9)}


def test_scale_is_parsed_kept_on_score_only_and_ignored_when_malformed():
    q = Question.parse(plan_dict()["rounds"][0]["questions"][0])
    assert q.scale == (0.0, 10.0) and q.to_dict()["scale"] == [0.0, 10.0]
    assert q.on_scale(0) == 0 and q.on_scale(4) == 10 and q.on_scale(2.4) == pytest.approx(6.0)
    for bad in ([0], [5, 5], ["a", 1], "0-10"):
        raw = dict(plan_dict()["rounds"][0]["questions"][0], scale=bad)
        assert Question.parse(raw).scale is None
    noul = Question.parse({"id": "n", "type": "noul", "instructions": "?", "scale": [0, 10]})
    assert noul.scale is None and "scale" not in noul.to_dict()


def test_the_mean_is_computed_in_code_and_readable_by_the_policy():
    plan = Plan.parse(plan_dict(), request=REQUEST)
    result = run_rounds(RecordedJevClient(ANSWERS), plan, {"articles": ARTICLES})
    assert result.item_scores() == {"q1": [3.4, 1.6, 3.0]}
    assert result.means()["q1"] == pytest.approx(8.0 / 3)
    outcome = run_policy(plan.policy, result.scopes())
    assert "report a leaning to rise" in outcome.actions
    with pytest.raises(PolicyError):
        Scope({}).resolve("mean.q9")


def test_an_aggregate_rule_fires_once_and_any_stays_per_item():
    # Evaluated in every scope, "mean.q1 >= 2" fired once per article too.
    plan = Plan.parse(plan_dict(), request=REQUEST)
    answers = dict(ANSWERS, **{"q1#a2": (1.6, 0.3)})  # a2 below the confidence floor
    result = run_rounds(RecordedJevClient(answers), plan, {"articles": ARTICLES})
    outcome = run_policy(plan.policy + (PolicyRule("count.q1 >= 1", "note"),),
                         result.scopes())
    fired = [(t.rule.then, t.item_key) for t in outcome.triggered]
    assert fired.count(("report a leaning to rise", None)) == 1
    assert fired.count(("note", None)) == 1
    assert ("escalate", "a2") in fired and ("escalate", "a1") not in fired


def test_the_synthesis_shows_each_item_and_the_mean_on_the_users_scale():
    llm = ScriptedLLMClient([json.dumps(plan_dict())])
    jev = _Staged({"shape": ("judgment", 0.96), "g3": 0.7}, ANSWERS)
    result = run(REQUEST, jev=jev, llm=llm, inputs={"articles": ARTICLES},
                 lint_with_jev=False, compiler_version="v0.5")
    text = result.text
    assert "a1: How strongly does 'item' point to chip stocks rising today -> 3.40" in text
    assert "→ 8.5 [0–10]" in text  # a1: 3.4 of 0..4
    assert "Media (3 elementi)" in text and "2.67 → 6.7 [0–10]" in text
    assert "(min 4.0, max 8.5)" in text


def test_without_a_scale_the_mean_stays_in_levels():
    llm = ScriptedLLMClient([json.dumps(plan_dict(scale=None))])
    jev = _Staged({"shape": ("judgment", 0.96), "g3": 0.7}, ANSWERS)
    text = run(REQUEST, jev=jev, llm=llm, inputs={"articles": ARTICLES},
               lint_with_jev=False).text
    assert "Media (3 elementi)" in text and ": 2.67 (min 1.60, max 3.40)" in text
    assert "→" not in text


def test_compiler_v0_5_adds_scale_and_means_and_v0_4_stays_the_default():
    assert COMPILER_PROMPT_VERSION == "v0.4"
    v4, v5 = compiler_system_prompt("v0.4"), compiler_system_prompt("v0.5")
    assert "scale" not in v4 and "mean.<qid>" not in v4
    assert "scale?" in v5 and "mean.<qid>" in v5 and '"da 0 a 10" -> [0, 10]' in v5


class _Staged:
    """Gate answers first, then the per-item scores."""

    model_version = "jev-1.13.0"

    def __init__(self, gate, rest):
        self.gate, self.rest, self.first = RecordedJevClient(gate), RecordedJevClient(rest), True

    def ask(self, state, questions):
        client, self.first = (self.gate if self.first else self.rest), False
        return client.ask(state, questions)
