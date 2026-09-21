"""The jev-drive loop, through the command line."""

from __future__ import annotations

import json

import pytest

from jev.benchmarks.plans import V26_PLAN
from jev.cli import main


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip().startswith("{") else out


def test_init_writes_the_transcript_and_reports_the_gate(tmp_path, capsys):
    state = tmp_path / "run.json"
    code, payload = run(capsys, "init", "--state", str(state),
                        "--request", "Traduci questo paragrafo in inglese.")
    assert code == 0
    assert payload["status"] == "pending"
    assert payload["call"]["purpose"] == "gate"
    assert state.exists()
    # The call says what shape of answer it wants, per question.
    assert payload["call"]["expects"]["g1"].startswith("a probability")


def test_init_refuses_to_clobber_an_existing_run(tmp_path, capsys):
    state = tmp_path / "run.json"
    run(capsys, "init", "--state", str(state), "--request", "Traduci questo paragrafo.")
    with pytest.raises(SystemExit):
        main(["init", "--state", str(state), "--request", "Altro."])


def test_next_before_init_says_so(tmp_path):
    with pytest.raises(SystemExit, match="jev-drive init"):
        main(["next", "--state", str(tmp_path / "missing.json")])


def test_a_full_loop_through_the_cli(tmp_path, capsys, monkeypatch):
    state = tmp_path / "run.json"
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"customer_message": "disdiciamo entro venerdì"}))

    _, payload = run(capsys, "init", "--state", str(state),
                     "--request", V26_PLAN.request, "--inputs", str(inputs))

    guard = 0
    while payload["status"] == "pending":
        guard += 1
        assert guard < 40, "the loop is not converging"
        call = payload["call"]
        answer = _answer(call)
        result_file = tmp_path / "answer.txt"
        result_file.write_text(answer)
        _, payload = run(capsys, "submit", "--state", str(state),
                         "--result", str(result_file),
                         "--fingerprint", call["fingerprint"])

    assert payload["status"] == "done"
    assert payload["jev_role"] == "pre+post"
    assert not payload["escalated"]

    code, text = run(capsys, "result", "--state", str(state))
    assert code == 0 and "Gentile cliente" in text


def test_result_before_the_end_exits_nonzero(tmp_path, capsys):
    state = tmp_path / "run.json"
    run(capsys, "init", "--state", str(state), "--request", "Traduci questo paragrafo.")
    assert main(["result", "--state", str(state)]) == 1


def test_a_plan_submitted_as_an_llm_answer_stays_verbatim(tmp_path, capsys):
    # The compiled plan is JSON, but it is the model's completion: it must not
    # be decoded on the way in.
    state = tmp_path / "run.json"
    run(capsys, "init", "--state", str(state), "--request", V26_PLAN.request)
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps(_gate_answer()))
    _, payload = run(capsys, "submit", "--state", str(state), "--result", str(gate))
    assert payload["call"]["purpose"] == "compile the plan"

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(V26_PLAN.to_json())
    _, payload = run(capsys, "submit", "--state", str(state), "--result", str(plan_file))
    assert payload["status"] == "pending"
    assert payload["call"]["purpose"].startswith("lint")

    recorded = json.loads(state.read_text())["records"][1]
    assert isinstance(recorded["result"], str)


def test_a_malformed_jev_answer_is_reported_not_raised(tmp_path, capsys):
    state = tmp_path / "run.json"
    run(capsys, "init", "--state", str(state), "--request", V26_PLAN.request)
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all")
    assert main(["submit", "--state", str(state), "--result", str(bad)]) == 2


def test_rewind_from_the_cli(tmp_path, capsys):
    state = tmp_path / "run.json"
    run(capsys, "init", "--state", str(state), "--request", V26_PLAN.request)
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps(_gate_answer()))
    run(capsys, "submit", "--state", str(state), "--result", str(gate))
    _, payload = run(capsys, "rewind", "--state", str(state))
    assert payload["call"]["purpose"] == "gate"


def _gate_answer():
    return {"shape": {"option": "mixed", "confidence": 0.99},
            "g1": 0.1, "g2": 0.1, "g3": 0.1, "g4": 0.1}


def _answer(call) -> str:
    if call["kind"] == "llm":
        if "compile" in call["purpose"]:
            return V26_PLAN.to_json()
        return "Gentile cliente, le confermo la tariffa attuale per dodici mesi."
    if call["purpose"] == "gate":
        return json.dumps(_gate_answer())
    if call["purpose"] == "round":
        return json.dumps({"q2": {"score": 3.0, "confidence": 0.9},
                           "q3": {"option": "price", "confidence": 0.9}})
    out = {}
    for q in call["questions"]:
        defect = q["id"].split("#")[0] in ("L9", "L11", "L12")
        out[q["id"]] = 0.05 if defect else 0.93
    return json.dumps(out)


def test_code_only_lint_skips_the_semantic_jev_calls(tmp_path, capsys):
    full = _purposes(tmp_path / "full.json", capsys, "full")
    code_only = _purposes(tmp_path / "code.json", capsys, "code")
    assert any(p.startswith("lint") for p in full)
    assert not any(p.startswith("lint") for p in code_only)
    assert len(code_only) < len(full)
    # Only the lint calls disappear; every other stage still runs.
    assert [p for p in full if not p.startswith("lint")] == code_only


def _purposes(state, capsys, lint):
    inputs = state.parent / f"inputs-{lint}.json"
    inputs.write_text(json.dumps({"customer_message": "disdiciamo entro venerdì"}))
    _, payload = run(capsys, "init", "--state", str(state), "--request", V26_PLAN.request,
                     "--inputs", str(inputs), "--lint", lint)
    purposes = []
    guard = 0
    while payload["status"] == "pending":
        guard += 1
        assert guard < 40
        call = payload["call"]
        purposes.append(call["purpose"])
        answer_file = state.parent / f"answer-{lint}.txt"
        answer_file.write_text(_answer(call))
        _, payload = run(capsys, "submit", "--state", str(state),
                         "--result", str(answer_file), "--fingerprint", call["fingerprint"])
    assert payload["status"] == "done"
    return purposes


def test_probe_emits_a_call_then_grades_its_answer(tmp_path, capsys):
    assert main(["probe"]) == 0
    call = json.loads(capsys.readouterr().out)
    assert [q["id"] for q in call["questions"]] == ["p1", "p2", "p3"]

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"p1": 0.94,
                                "p2": {"score": 2.3, "confidence": 0.88},
                                "p3": {"option": "price", "confidence": 0.91}}))
    assert main(["probe", "--check", str(good)]) == 0
    assert "map cleanly" in capsys.readouterr().out

    poor = tmp_path / "poor.json"
    poor.write_text(json.dumps({"p1": 0.94, "p2": 2, "p3": "price"}))
    assert main(["probe", "--check", str(poor)]) == 1
    assert "warning:" in capsys.readouterr().out
