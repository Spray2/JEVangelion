"""The gate: role derivation, and a replay of the runs recorded in docs/spec.md."""

from __future__ import annotations

import pytest

from jev.benchmarks.cases import BENCH_V1, BENCH_V2, BORDERLINE, by_id
from jev.client import RecordedJevClient
from jev.contract import JevRole
from jev.eval import run_bench
from jev.gate import GATE_V2, GATE_V3, COST_THRESHOLD, run_gate


def test_v1_bench_replays_15_of_15():
    report = run_bench(BENCH_V1, GATE_V2, dataset="v1")
    assert report.shape_score == (15, 15)
    assert report.role_score == (15, 15)


def test_v2_bench_replays_30_of_30():
    report = run_bench(BENCH_V2, GATE_V2, dataset="v2")
    assert report.shape_score == (30, 30)
    assert report.role_score == (30, 30)


def test_borderline_set_reproduces_the_published_scores():
    v2 = run_bench(BORDERLINE, GATE_V2, dataset="borderline")
    v3 = run_bench(BORDERLINE, GATE_V3, dataset="borderline")
    # "concorda con l'umano sulla forma in 11 casi su 12": H02 is the exception.
    assert v2.shape_score == (11, 12)
    assert v2.role_score == (6, 12)
    assert v3.role_score == (9, 12)


@pytest.mark.parametrize("case_id", [c.id for c in BORDERLINE])
def test_each_borderline_case_matches_its_recorded_decision(case_id):
    case = by_id(case_id)
    for variant, recorded in ((GATE_V2, case.gate_v2), (GATE_V3, case.gate_v3)):
        if recorded is None:
            continue
        decision = run_gate(RecordedJevClient(case.recorded_answers()), case.request, variant)
        got = "escalation" if decision.escalate else (
            decision.jev_role.value if variant is GATE_V3
            else f"{decision.task_shape.value}/{decision.jev_role.value}"
        )
        assert got == recorded, f"{case_id} on gate {variant.version}"


def test_judgment_needs_one_condition_above_threshold():
    client = RecordedJevClient(
        {"shape": ("judgment", 0.99), "g1": 0.49, "g2": 0.49, "g3": 0.49, "g4": 0.99}
    )
    assert run_gate(client, "x", GATE_V2).jev_role is JevRole.NONE


def test_v2_escalates_on_an_unsure_shape():
    client = RecordedJevClient({"shape": ("generative", 0.36), "g4": 0.9})
    decision = run_gate(client, "x", GATE_V2)
    assert decision.escalate and decision.jev_role is JevRole.NONE


def test_v3_does_not_escalate_on_an_unsure_shape():
    client = RecordedJevClient({"shape": ("extraction", 0.44), "cost": (2.98, 0.9)})
    decision = run_gate(client, "x", GATE_V3)
    assert not decision.escalate
    assert decision.jev_role is JevRole.POST


def test_v3_core_never_also_gets_post():
    client = RecordedJevClient(
        {"shape": ("judgment", 0.99), "g3": 0.9, "cost": (2.9, 0.9), "pre": 0.9}
    )
    assert run_gate(client, "x", GATE_V3).jev_role is JevRole.CORE


def test_v3_cost_threshold_is_the_only_post_signal():
    just_below = RecordedJevClient(
        {"shape": ("generative", 0.99), "cost": (COST_THRESHOLD - 0.01, 0.9), "pre": 0.1}
    )
    just_above = RecordedJevClient(
        {"shape": ("generative", 0.99), "cost": (COST_THRESHOLD, 0.9), "pre": 0.1}
    )
    assert run_gate(just_below, "x", GATE_V3).jev_role is JevRole.NONE
    assert run_gate(just_above, "x", GATE_V3).jev_role is JevRole.POST


def test_the_gate_is_one_call_with_five_questions():
    client = RecordedJevClient({"shape": ("extraction", 1.0)})
    run_gate(client, "Qual è la data di scadenza?", GATE_V2)
    assert len(client.calls) == 1
    state, question_ids = client.calls[0]
    assert state == {"request": "Qual è la data di scadenza?"}
    assert question_ids == ("shape", "g1", "g2", "g3", "g4")


def test_mixed_is_always_pre_post_in_v2():
    client = RecordedJevClient({"shape": ("mixed", 0.99)})
    assert run_gate(client, "x", GATE_V2).jev_role is JevRole.PRE_POST


def test_extraction_is_always_none_in_v2():
    client = RecordedJevClient({"shape": ("extraction", 0.99), "g1": 0.99, "g3": 0.99})
    assert run_gate(client, "x", GATE_V2).jev_role is JevRole.NONE
