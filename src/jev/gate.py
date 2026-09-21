"""The JEV gate: does this request need a plan at all?

One ``jev_ask`` per request — one state (the request text), five independent
questions — and the role is derived in code with a 0.5 threshold. Two variants
live here:

* :data:`GATE_V2` — the validated one. 42/42 on task_shape and 30/30 on
  jev_role on the v2 bench, but only 6/12 against the human annotator, where it
  errs by missing verification.
* :data:`GATE_V3` — three independent flags (core / pre / post) instead of a
  role derived from the shape. It scores 9/12 on the same human set, but its
  1.75 ``cost`` threshold was tuned after seeing those labels: it must be
  revalidated on new borderline cases before it is adopted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .client import JevClient
from .contract import JevRole, TaskShape
from .primitives import (
    Answer,
    ChoiceOption,
    Question,
    QuestionRole,
    QuestionType,
    ScoreLevel,
)

#: Below this confidence on the shape Choice the gate escalates instead of
#: guessing. H07 (0.36) and H10 (0.44) are the two recorded escalations.
SHAPE_CONFIDENCE_FLOOR = 0.5

#: Threshold for the g1-g4 Nouls and for the v3 ``pre`` Noul.
SIGNAL_THRESHOLD = 0.5

#: Threshold on the v3 ``cost`` Score. Tuned a posteriori on 12 cases.
COST_THRESHOLD = 1.75


def _noul(qid: str, text: str) -> Question:
    return Question(id=qid, type=QuestionType.NOUL, instructions=text, role=QuestionRole.CORE)


SHAPE_QUESTION = Question(
    id="shape",
    type=QuestionType.CHOICE,
    role=QuestionRole.CORE,
    instructions="What shape does `request` have?",
    criteria=(
        ChoiceOption(
            "judgment",
            "The answer IS a decision, rating or category over content the user gives.",
        ),
        ChoiceOption(
            "generative",
            "The deliverable is new text, code, an explanation or a document.",
        ),
        ChoiceOption(
            "mixed",
            "A decision must be made first, then text is generated based on it.",
        ),
        ChoiceOption(
            "extraction",
            "The answer is an open fact: a name, a number, a date, a value.",
        ),
    ),
)

G1 = _noul(
    "g1",
    "Does `request` ask for three or more different kinds of assessment about each item? "
    "Count kinds of question, not number of items.",
)
G2 = _noul(
    "g2",
    "Is the purpose of `request` to decide what happens next to the items "
    "(approve, reject, block, escalate, assign to someone), rather than to produce "
    "information or text?",
)
G3 = _noul(
    "g3",
    "Does `request` refer to an open collection of items (plural nouns such as tickets, "
    "emails, commits) each needing the same judgment, rather than a single item or a fixed "
    "set of three or fewer?",
)
# g4 is unchanged from v1; docs/spec.md records its role but not its literal v1
# wording, so this is the wording used here and by the bench in this repo.
G4 = _noul(
    "g4",
    "Is the output of `request` new text going to an external or high-stakes recipient "
    "(a client, an employer, a board, management, or publication), so that it must be "
    "verified before it is delivered?",
)

#: v3 only: the "pre" flag, limited to generative requests (it does not
#: discriminate on judgments, where it stays between 0.56 and 0.88).
PRE_QUESTION = _noul(
    "pre",
    "Must a decision or condition about the input be evaluated before the output "
    "can be written?",
)

#: v3 only: replaces g4. Measures the cost of an undetected error, not the recipient.
COST_QUESTION = Question(
    id="cost",
    type=QuestionType.SCORE,
    role=QuestionRole.CORE,
    instructions="How costly is an undetected error in the answer to `request`?",
    criteria=(
        ScoreLevel("Trivial: the user notices and fixes it at no cost.",
                   ("a rephrased sentence is clumsy",)),
        ScoreLevel("Annoying: it wastes some time or looks unprofessional.",
                   ("a summary misses a minor point",)),
        ScoreLevel("Costly: it reaches a third party or drives a decision that is hard to undo.",
                   ("a wrong deadline is missed", "a client receives a wrong figure")),
        ScoreLevel("Harmful: it causes legal, financial or safety damage.",
                   ("a contract is signed with an unnoticed liability clause",)),
    ),
)


@dataclass(frozen=True)
class GateDecision:
    task_shape: TaskShape
    jev_role: JevRole
    rationale: str
    escalate: bool = False
    signals: dict[str, float] = field(default_factory=dict)
    shape_confidence: float = 0.0
    version: str = "v2"

    @property
    def needs_plan(self) -> bool:
        """The compiler is invoked only when the gate returns a role."""
        return self.jev_role is not JevRole.NONE


@dataclass(frozen=True)
class GateVariant:
    version: str
    questions: tuple[Question, ...]

    def decide(self, answers: dict[str, Answer]) -> GateDecision:
        raise NotImplementedError


class _GateV2(GateVariant):
    def decide(self, answers: dict[str, Answer]) -> GateDecision:
        shape, confidence = _read_shape(answers)
        signals = _read_signals(answers, ("g1", "g2", "g3", "g4"))
        if confidence < SHAPE_CONFIDENCE_FLOOR:
            return GateDecision(
                shape, JevRole.NONE,
                f"shape confidence {confidence:.2f} below {SHAPE_CONFIDENCE_FLOOR}",
                escalate=True, signals=signals, shape_confidence=confidence, version=self.version,
            )
        if shape is TaskShape.EXTRACTION:
            role, why = JevRole.NONE, "extraction is always none in v2"
        elif shape is TaskShape.MIXED:
            role, why = JevRole.PRE_POST, "every mixed request gets pre+post"
        elif shape is TaskShape.JUDGMENT:
            strongest = max(signals.get(k, 0.0) for k in ("g1", "g2", "g3"))
            core = strongest > SIGNAL_THRESHOLD
            role = JevRole.CORE if core else JevRole.NONE
            why = f"max(g1,g2,g3) = {strongest:.2f}"
        else:
            g4 = signals.get("g4", 0.0)
            role = JevRole.POST if g4 > SIGNAL_THRESHOLD else JevRole.NONE
            why = f"g4 = {g4:.2f}"
        return GateDecision(shape, role, why, signals=signals,
                            shape_confidence=confidence, version=self.version)


class _GateV3(GateVariant):
    def decide(self, answers: dict[str, Answer]) -> GateDecision:
        shape, confidence = _read_shape(answers)
        signals = _read_signals(answers, ("g1", "g2", "g3", "pre"))
        cost_answer = answers.get("cost")
        cost = float(cost_answer.score) if cost_answer and cost_answer.score is not None else 0.0
        signals["cost"] = cost

        # v3 does not escalate on an unsure shape. The three flags are
        # independent: only "core" and "pre" read the shape at all, and "post"
        # stands on the cost score alone. That is what recovers H07 and H10,
        # the two cases v2 escalated.
        core = shape is TaskShape.JUDGMENT and max(
            signals.get(k, 0.0) for k in ("g1", "g2", "g3")
        ) > SIGNAL_THRESHOLD
        pre = shape is TaskShape.MIXED or (
            shape is TaskShape.GENERATIVE and signals.get("pre", 0.0) > SIGNAL_THRESHOLD
        )
        post = cost >= COST_THRESHOLD

        if core:
            # A core judgment is the deliverable; it does not also get a post check.
            role, why = JevRole.CORE, "core conditions met on a judgment"
        elif pre and post:
            role, why = JevRole.PRE_POST, f"pre flag set and cost {cost:.2f} >= {COST_THRESHOLD}"
        elif pre:
            role, why = JevRole.PRE, "pre flag set, cost below threshold"
        elif post:
            role, why = JevRole.POST, f"cost {cost:.2f} >= {COST_THRESHOLD}"
        else:
            role, why = JevRole.NONE, f"no flag set (cost {cost:.2f})"
        return GateDecision(shape, role, why, signals=signals,
                            shape_confidence=confidence, version=self.version)


GATE_V2 = _GateV2("v2", (SHAPE_QUESTION, G1, G2, G3, G4))
GATE_V3 = _GateV3("v3", (SHAPE_QUESTION, G1, G2, G3, PRE_QUESTION, COST_QUESTION))


def run_gate(client: JevClient, request: str, variant: GateVariant = GATE_V2) -> GateDecision:
    """One ``jev_ask``: state is the request, questions are the gate's own."""
    answers = client.ask({"request": request}, variant.questions)
    return variant.decide(answers)


def _read_shape(answers: dict[str, Answer]) -> tuple[TaskShape, float]:
    a = answers.get("shape")
    if a is None or a.option is None:
        raise KeyError("gate answers are missing the 'shape' choice")
    return TaskShape(a.option), float(a.confidence or 0.0)


def _read_signals(answers: dict[str, Answer], ids: Sequence[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for qid in ids:
        a = answers.get(qid)
        if a is not None and a.probability is not None:
            out[qid] = float(a.probability)
    return out
