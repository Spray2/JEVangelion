"""Driver mode: the pipeline asks for model calls instead of making them."""

from __future__ import annotations

import json

import pytest

from jev.benchmarks.cases import by_id
from jev.benchmarks.plans import V26_PLAN
from jev.driver import Call, Driver, DriverError, Transcript, parse_answers
from jev.pipeline import PipelineResult
from jev.primitives import Question, QuestionType, ChoiceOption, ScoreLevel


def gate_answers(shape="mixed", **signals):
    out = {"shape": {"option": shape, "confidence": 0.99}}
    for key in ("g1", "g2", "g3", "g4"):
        out[key] = signals.get(key, 0.0)
    return out


def lint_answers(call):
    return {
        q["id"]: (0.05 if q["id"].split("#")[0] in ("L9", "L11", "L12") else 0.93)
        for q in call.questions
    }


def drive(transcript, answer):
    """Run to completion, answering every call with `answer(call)`."""
    driver = Driver(transcript)
    calls = []
    step = driver.step()
    while isinstance(step, Call):
        calls.append(step)
        step = driver.submit(answer(step), step.fingerprint)
    return step, calls


def test_the_first_call_is_always_the_gate():
    driver = Driver(Transcript(request="Traduci questo paragrafo in inglese."))
    call = driver.step()
    assert isinstance(call, Call)
    assert call.kind == "jev" and call.purpose == "gate"
    assert [q["id"] for q in call.questions] == ["shape", "g1", "g2", "g3", "g4"]
    assert call.state == {"request": "Traduci questo paragrafo in inglese."}


def test_a_none_role_finishes_after_the_gate_and_one_generation():
    case = by_id("V05")

    def answer(call):
        if call.purpose == "gate":
            return gate_answers("generative", g4=0.08)
        return "OAuth2 funziona così."

    result, calls = drive(Transcript(request=case.request), answer)
    assert isinstance(result, PipelineResult)
    assert [c.purpose for c in calls] == ["gate", "generate the text"]
    assert result.text == "OAuth2 funziona così."


def test_a_full_pre_post_run_asks_for_every_stage_in_order():
    def answer(call):
        if call.kind == "llm":
            return V26_PLAN.to_json() if "compile" in call.purpose else "Gentile cliente..."
        if call.purpose == "gate":
            return gate_answers("mixed")
        if call.purpose.startswith("lint"):
            return lint_answers(call)
        if call.purpose == "round":
            return {"q2": {"score": 3.0, "confidence": 0.9},
                    "q3": {"option": "price", "confidence": 0.91}}
        return {q["id"]: 0.9 for q in call.questions}

    transcript = Transcript(
        request=V26_PLAN.request,
        inputs={"customer_message": "disdiciamo entro venerdì"},
    )
    result, calls = drive(transcript, answer)
    kinds = [c.purpose for c in calls]
    assert kinds[0] == "gate"
    assert kinds[1] == "compile the plan"
    assert any(k.startswith("lint") for k in kinds)
    assert "round" in kinds
    assert "generate the text" in kinds
    assert "post checks" in kinds
    assert isinstance(result, PipelineResult) and not result.escalated
    assert "price" in result.text


def test_the_transcript_survives_a_round_trip_through_json():
    driver = Driver(Transcript(request="Traduci questo paragrafo in inglese."))
    driver.submit(gate_answers("generative", g4=0.08))
    restored = Driver(Transcript.from_json(driver.transcript.to_json()))
    step = restored.step()
    assert isinstance(step, Call) and step.purpose == "generate the text"


def test_an_answer_for_another_call_is_refused():
    driver = Driver(Transcript(request="Traduci questo paragrafo."))
    with pytest.raises(DriverError, match="pending"):
        driver.submit(gate_answers(), fingerprint="deadbeefcafe")


def test_changing_the_request_invalidates_the_recorded_answers():
    driver = Driver(Transcript(request="Traduci questo paragrafo in inglese."))
    driver.submit(gate_answers("generative", g4=0.08))
    driver.transcript.request = "Tutt'altra richiesta."
    with pytest.raises(DriverError, match="changed since it was recorded"):
        driver.step()


def test_rewind_undoes_the_last_answer():
    driver = Driver(Transcript(request="Traduci questo paragrafo in inglese."))
    driver.submit(gate_answers("generative", g4=0.08))
    assert driver.step().purpose == "generate the text"
    driver.rewind()
    assert driver.step().purpose == "gate"


def test_a_wrong_answer_kind_names_the_mismatch():
    driver = Driver(Transcript(request="Traduci questo paragrafo."))
    driver.transcript.records.append({"kind": "llm", "fingerprint": None, "result": "testo"})
    with pytest.raises(DriverError, match="belongs to another run"):
        driver.step()


def test_submitting_after_the_run_is_finished_is_refused():
    def answer(call):
        return gate_answers("generative", g4=0.08) if call.kind == "jev" else "testo"

    transcript = Transcript(request="Traduci questo paragrafo in inglese.")
    result, _ = drive(transcript, answer)
    assert isinstance(result, PipelineResult)
    with pytest.raises(DriverError, match="already finished"):
        Driver(transcript).submit("altro")


# -- the tolerant answer parser -------------------------------------------

NOUL = Question(id="q1", type=QuestionType.NOUL, instructions="Is it so?")
SCORE = Question(id="q2", type=QuestionType.SCORE, instructions="How severe?",
                 criteria=(ScoreLevel("low", ("x",)), ScoreLevel("high", ("y",))))
CHOICE = Question(id="q3", type=QuestionType.CHOICE, instructions="Which?",
                  criteria=(ChoiceOption("price"), ChoiceOption("other")))


@pytest.mark.parametrize("payload", [
    {"q1": 0.87},
    {"q1": {"probability": 0.87}},
    {"q1": {"p": 0.87}},
    {"q1": {"value": 0.87}},
    [{"id": "q1", "probability": 0.87}],
    {"answers": {"q1": 0.87}},
    '{"q1": 0.87}',
])
def test_a_noul_answer_is_read_from_any_of_the_usual_shapes(payload):
    answers = parse_answers([NOUL], payload)
    assert answers["q1"].probability == pytest.approx(0.87)


def test_a_score_keeps_its_fractional_expected_level():
    answers = parse_answers([SCORE], {"q2": {"score": 2.17, "confidence": 0.9}})
    assert answers["q2"].score == pytest.approx(2.17)
    assert answers["q2"].level == 2


def test_a_choice_reads_option_and_confidence():
    answers = parse_answers([CHOICE], {"q3": {"option": "price", "confidence": 0.91}})
    assert answers["q3"].option == "price" and answers["q3"].confidence == 0.91
    assert parse_answers([CHOICE], {"q3": "price"})["q3"].option == "price"


def test_a_missing_answer_names_the_question():
    with pytest.raises(DriverError, match="q1"):
        parse_answers([NOUL], {"q9": 0.5})


def test_a_noul_without_a_probability_is_refused():
    with pytest.raises(DriverError, match="needs a probability"):
        parse_answers([NOUL], {"q1": {"confidence": 0.9}})


def test_a_boolean_answers_a_noul_but_not_a_score():
    assert parse_answers([NOUL], {"q1": True})["q1"].probability == 1.0
    with pytest.raises(DriverError, match="only answers a noul"):
        parse_answers([SCORE], {"q2": True})


# -- the schema probe -----------------------------------------------------


def test_the_probe_covers_all_three_primitives():
    from jev.driver import probe_call

    call = probe_call()
    assert [q["type"] for q in call["questions"]] == ["noul", "score", "choice"]
    assert set(call["expects"]) == {"p1", "p2", "p3"}


def test_a_well_shaped_answer_raises_no_warning():
    from jev.driver import read_probe

    reading = read_probe({
        "p1": 0.94,
        "p2": {"score": 2.3, "confidence": 0.88},
        "p3": {"option": "price", "confidence": 0.91},
    })
    assert reading.warnings == []
    assert reading.answers["p2"].score == pytest.approx(2.3)


def test_the_probe_flags_an_integer_only_score_and_missing_confidences():
    from jev.driver import read_probe

    reading = read_probe({"p1": 0.94, "p2": 2, "p3": "price"})
    joined = " ".join(reading.warnings)
    assert "whole number" in joined and "1.75" in joined
    assert joined.count("without a confidence") == 2


def test_the_probe_flags_an_inverted_noul():
    from jev.driver import read_probe

    reading = read_probe({
        "p1": 0.03,
        "p2": {"score": 2.3, "confidence": 0.9},
        "p3": {"option": "price", "confidence": 0.9},
    })
    assert any("polarity" in w for w in reading.warnings)


def test_the_probe_flags_an_option_outside_the_taxonomy():
    from jev.driver import read_probe

    reading = read_probe({
        "p1": 0.94,
        "p2": {"score": 2.3, "confidence": 0.9},
        "p3": {"option": "prezzo", "confidence": 0.9},
    })
    assert any("not one of the option ids" in w for w in reading.warnings)
