"""The driver as MCP tools: same loop as jev-drive, runs kept by id."""

from __future__ import annotations

import json

import pytest

from jev.benchmarks.plans import V26_PLAN
from jev.driver import DriverError
from jev.mcp_server import (
    jev_ask_params,
    jev_next,
    jev_result,
    jev_rewind,
    jev_start,
    jev_submit,
)


@pytest.fixture(autouse=True)
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_RUNS_DIR", str(tmp_path))
    return tmp_path


def wire_answer(params):
    """What typesafe-jev would return for `params`, in its own wire shape."""
    answers = {}
    for qid, q in params["questions"].items():
        if q["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": 0.9}
        elif q["type"] == "score":
            answers[qid] = {"type": "score", "score": 2.0, "confidence": 0.9}
        else:
            # V26 is a mixed request; elsewhere the first option will do.
            options = list(q["criteria"])
            pick = "mixed" if "mixed" in options else options[0]
            answers[qid] = {"type": "choice", "choice": pick, "confidence": 0.9}
    # As a chat client hands an MCP tool result over: wrapped, as JSON text.
    return {"result": json.dumps({"model": "jev-1.13.0", "answers": answers,
                                  "usage": {}, "meta": {}})}


def test_questions_are_translated_to_the_typesafe_jev_shape():
    params = jev_ask_params({"text": "x"}, [
        {"id": "n", "type": "noul", "instructions": "?", "criteria": None},
        {"id": "s", "type": "score", "instructions": "?",
         "criteria": [{"what": "low", "examples": ["a"]}, {"what": "high"}]},
        {"id": "c", "type": "choice", "instructions": "?",
         "criteria": [{"id": "price", "what": "cost"}, {"id": "other", "what": None}]},
    ])
    assert params["state"] == {"text": "x"}
    assert params["questions"]["n"] == {"type": "noul", "instructions": "?"}
    assert params["questions"]["s"]["criteria"] == [
        {"what": "low", "examples": ["a"]}, {"what": "high", "examples": []}
    ]
    assert params["questions"]["c"]["criteria"] == {"price": "cost", "other": None}


def test_a_full_run_through_the_tools(runs_dir):
    step = jev_start(V26_PLAN.request, {"customer_message": "disdiciamo il contratto"})
    run_id = step["run_id"]
    assert (runs_dir / f"{run_id}.json").exists()

    kinds = []
    while step["status"] == "pending":
        call = step["call"]
        kinds.append(call["kind"])
        if call["kind"] == "jev":
            answer = wire_answer(call["jev_ask_params"])
        elif call["purpose"] == "compile the plan":
            answer = V26_PLAN.to_json()
        else:
            answer = "Gentile cliente, restiamo a disposizione."
        step = jev_submit(run_id, call["fingerprint"], answer)

    assert kinds[:2] == ["jev", "llm"]  # gate, then the compiler
    assert step["status"] == "done" and step["text"]
    assert jev_result(run_id)["text"] == step["text"]
    assert jev_next(run_id)["status"] == "done"


def test_a_wrong_fingerprint_and_a_rewind(runs_dir):
    step = jev_start(V26_PLAN.request, {"customer_message": "disdiciamo"})
    run_id, call = step["run_id"], step["call"]
    with pytest.raises(DriverError):
        jev_submit(run_id, "not-this-call", wire_answer(call["jev_ask_params"]))

    after = jev_submit(run_id, call["fingerprint"], wire_answer(call["jev_ask_params"]))
    assert after["call"]["kind"] == "llm"
    back = jev_rewind(run_id)
    assert back["call"]["fingerprint"] == call["fingerprint"]


def test_run_ids_cannot_escape_the_runs_dir():
    with pytest.raises(DriverError):
        jev_next("../secrets")
