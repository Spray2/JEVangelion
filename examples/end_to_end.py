"""A full run with stub models, so the pipeline can be watched without a network.

    python examples/end_to_end.py

Bind the real thing by replacing the two clients: a ``JevClient`` over
``jev_ask`` of the pinned model, and an ``LLMClient`` over the generative model.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jev.benchmarks.cases import by_id
from jev.benchmarks.plans import V26_PLAN
from jev.client import RecordedJevClient, ScriptedLLMClient
from jev.pipeline import run

REPLY = (
    "Gentile cliente, mi dispiace per l'aumento di prezzo che ha ricevuto. "
    "Le confermo che possiamo mantenere la tariffa attuale per dodici mesi e "
    "le propongo una revisione del contratto entro venerdì."
)


class Stub:
    """Gate answers first, then everything else at a fixed value."""

    model_version = "jev-1.13.0"

    def __init__(self, gate_answers, rest_answers):
        self.gate = RecordedJevClient(gate_answers)
        self.rest = RecordedJevClient(rest_answers, default_noul=0.9)
        self.first = True

    def ask(self, state, questions):
        if self.first:
            self.first = False
            return self.gate.ask(state, questions)
        return self.rest.ask(state, questions)


def main() -> None:
    case = by_id("V26")
    jev = Stub(
        case.recorded_answers(),
        {"L9": 0.05, "L11": 0.05, "L12": 0.05, "q2": (2.0, 0.88), "q3": ("price", 0.91)},
    )
    llm = ScriptedLLMClient([V26_PLAN.to_json(), REPLY])

    result = run(
        case.request,
        jev=jev,
        llm=llm,
        inputs={"customer_message": "Se il prezzo non cambia entro venerdì disdiciamo."},
    )

    print(f"gate      {result.gate.task_shape.value} / {result.gate.jev_role.value} "
          f"({result.gate.rationale})")
    print(f"lint      {len(result.lint.checks)} checks, "
          f"{len(result.lint.failures)} failed, blocked={result.lint.blocked}")
    print(f"rounds    {result.rounds.calls} jev_ask call(s), "
          f"{len(result.rounds.answers)} answers")
    print(f"policy    {result.policy.actions}")
    print(f"verify    {[(o.id, o.passed) for o in result.verification.outcomes]}")
    print()
    print("--- prompt given to the main LLM " + "-" * 40)
    print(llm.prompts[1][1])
    print()
    print("--- answer given to the user " + "-" * 44)
    print(result.text)


if __name__ == "__main__":
    main()
