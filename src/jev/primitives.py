"""JEV primitives: the three question types and the answers they produce.

The shapes here mirror the plan contract in docs/spec.md. A question is either a
Noul (presence / conformity, "yes" is always the high value), a Score (an ordered
magnitude, levels from lowest to highest) or a Choice (a closed taxonomy).
Anything whose answer set is open is extraction and is not a JEV question at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

#: The JEV model the thresholds in this package are tuned for (see docs/spec.md).
JEV_MODEL_VERSION = "jev-1.13.0"

#: A Noul carries no confidence of its own; the runtime treats this band as
#: "uncertain" and carries it to the synthesis as "da verificare".
NOUL_UNCERTAIN_BAND = (0.4, 0.6)

#: Below this confidence the policy escalates. Never hard-coded in a question.
CONFIDENCE_FLOOR = 0.5


class QuestionType(str, Enum):
    NOUL = "noul"
    SCORE = "score"
    CHOICE = "choice"


class QuestionRole(str, Enum):
    PRE = "pre"
    CORE = "core"
    POST = "post"


@dataclass(frozen=True)
class ScoreLevel:
    """One level of a Score, anchored by a description and at least one example."""

    what: str
    examples: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: Any) -> "ScoreLevel":
        if isinstance(raw, str):
            # Tolerated on input so a malformed plan can still be linted (L3 fails).
            return cls(what=raw, examples=())
        examples = raw.get("examples") or []
        if isinstance(examples, str):
            examples = [examples]
        return cls(what=raw.get("what", ""), examples=tuple(examples))

    def to_dict(self) -> dict[str, Any]:
        return {"what": self.what, "examples": list(self.examples)}


@dataclass(frozen=True)
class ChoiceOption:
    id: str
    what: str = ""

    @classmethod
    def parse(cls, raw: Any) -> "ChoiceOption":
        if isinstance(raw, str):
            return cls(id=raw, what="")
        return cls(id=raw.get("id", ""), what=raw.get("what", ""))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "what": self.what}


@dataclass(frozen=True)
class Question:
    """A single JEV question.

    ``for_each`` and ``criteria_from`` hold a placeholder of a runtime collection:
    the question is a template the runtime instantiates once the input arrives.
    """

    id: str
    type: QuestionType
    instructions: str
    role: QuestionRole = QuestionRole.CORE
    criteria: tuple[Any, ...] | None = None
    for_each: str | None = None
    criteria_from: str | None = None

    @property
    def is_template(self) -> bool:
        return self.for_each is not None or self.criteria_from is not None

    @property
    def levels(self) -> tuple[ScoreLevel, ...]:
        if self.type is not QuestionType.SCORE or not self.criteria:
            return ()
        return tuple(c for c in self.criteria if isinstance(c, ScoreLevel))

    @property
    def options(self) -> tuple[ChoiceOption, ...]:
        if self.type is not QuestionType.CHOICE or not self.criteria:
            return ()
        return tuple(c for c in self.criteria if isinstance(c, ChoiceOption))

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "Question":
        qtype = QuestionType(raw["type"])
        # The compiler prompt calls the field "criteria"; "options" is accepted
        # because plans in the wild (and docs/spec.md's own V26 example) use it.
        raw_criteria = raw.get("criteria")
        if raw_criteria is None:
            raw_criteria = raw.get("options")
        criteria: tuple[Any, ...] | None
        if qtype is QuestionType.SCORE and raw_criteria:
            criteria = tuple(ScoreLevel.parse(c) for c in raw_criteria)
        elif qtype is QuestionType.CHOICE and raw_criteria:
            criteria = tuple(ChoiceOption.parse(c) for c in raw_criteria)
        else:
            criteria = None
        role = raw.get("role") or QuestionRole.CORE.value
        return cls(
            id=raw["id"],
            type=qtype,
            instructions=raw.get("instructions", ""),
            role=QuestionRole(role),
            criteria=criteria,
            for_each=raw.get("for_each"),
            criteria_from=raw.get("criteria_from"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type.value,
            "role": self.role.value,
            "instructions": self.instructions,
        }
        if self.type is QuestionType.SCORE:
            out["criteria"] = [level.to_dict() for level in self.levels]
        elif self.type is QuestionType.CHOICE:
            out["criteria"] = [opt.to_dict() for opt in self.options]
        else:
            out["criteria"] = None
        if self.for_each is not None:
            out["for_each"] = self.for_each
        if self.criteria_from is not None:
            out["criteria_from"] = self.criteria_from
        return out


@dataclass(frozen=True)
class Answer:
    """One JEV answer.

    A Noul answers with ``probability`` only: it has no confidence of its own, so
    the runtime reads :data:`NOUL_UNCERTAIN_BAND` instead. Score and Choice carry
    a calibrated ``confidence``.
    """

    question_id: str
    type: QuestionType
    probability: float | None = None
    #: The expected level of a Score. JEV returns a probability-weighted mean, so
    #: this is fractional (the gate's ``cost`` score compares it against 1.75).
    score: float | None = None
    option: str | None = None
    confidence: float | None = None
    #: Set when the answer comes from a template question instantiated at runtime.
    item_key: str | None = None
    item_index: int | None = None

    @property
    def level(self) -> int | None:
        """The nearest discrete Score level."""
        return None if self.score is None else int(round(self.score))

    @property
    def is_uncertain(self) -> bool:
        """True when the answer sits in the band the synthesis flags for review."""
        if self.type is QuestionType.NOUL and self.probability is not None:
            low, high = NOUL_UNCERTAIN_BAND
            return low <= self.probability <= high
        if self.confidence is not None:
            return self.confidence < CONFIDENCE_FLOOR
        return False

    @property
    def value(self) -> float | str | None:
        """The operand a bare ``qN`` resolves to in a policy expression."""
        if self.type is QuestionType.NOUL:
            return self.probability
        if self.type is QuestionType.SCORE:
            return self.score
        return self.option


def noul(question_id: str, probability: float, **kw: Any) -> Answer:
    return Answer(question_id, QuestionType.NOUL, probability=probability, **kw)


def score(question_id: str, level: float, confidence: float, **kw: Any) -> Answer:
    return Answer(question_id, QuestionType.SCORE, score=float(level), confidence=confidence, **kw)


def choice(question_id: str, option: str, confidence: float, **kw: Any) -> Answer:
    return Answer(question_id, QuestionType.CHOICE, option=option, confidence=confidence, **kw)
