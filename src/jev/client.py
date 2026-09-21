"""Ports to the two external systems: JEV itself and the main LLM.

Nothing in this package talks to a network. A host binds :class:`JevClient` to
the real ``jev_ask`` of the pinned model and :class:`LLMClient` to whatever
generative model it runs; the fakes below make the whole pipeline testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .primitives import JEV_MODEL_VERSION, Answer, Question, QuestionType


class JevClient(Protocol):
    """One ``jev_ask`` call: one state, many questions."""

    model_version: str

    def ask(self, state: Any, questions: Sequence[Question]) -> dict[str, Answer]:
        ...


class LLMClient(Protocol):
    """The main LLM, used by the compiler and by generation."""

    def complete(self, system: str, user: str) -> str:
        ...


class JevVersionMismatch(RuntimeError):
    """The bound client does not run the model the thresholds are tuned for."""


def check_pinned(client: JevClient, expected: str = JEV_MODEL_VERSION) -> None:
    got = getattr(client, "model_version", None)
    if got != expected:
        raise JevVersionMismatch(
            f"thresholds in this package are tuned for {expected}, client reports {got!r}"
        )


@dataclass
class CallableJevClient:
    """Adapter over a host-supplied ``jev_ask`` function."""

    fn: Callable[[Any, Sequence[Question]], dict[str, Answer]]
    model_version: str = JEV_MODEL_VERSION

    def ask(self, state: Any, questions: Sequence[Question]) -> dict[str, Answer]:
        return self.fn(state, questions)


@dataclass
class CallableLLMClient:
    fn: Callable[[str, str], str]

    def complete(self, system: str, user: str) -> str:
        return self.fn(system, user)


@dataclass
class RecordedJevClient:
    """Replays answers keyed by question id, for tests and for replaying a run.

    ``answers`` maps a question id to a value: a float for a Noul, a
    ``(level, confidence)`` pair for a Score, an ``(option, confidence)`` pair for
    a Choice. Template questions are keyed ``"<qid>#<item_key>"`` and fall back to
    the bare id when no per-item value is recorded.
    """

    answers: dict[str, Any] = field(default_factory=dict)
    model_version: str = JEV_MODEL_VERSION
    default_noul: float = 0.0
    default_confidence: float = 0.9
    calls: list[tuple[Any, tuple[str, ...]]] = field(default_factory=list)

    def ask(self, state: Any, questions: Sequence[Question]) -> dict[str, Answer]:
        self.calls.append((state, tuple(q.id for q in questions)))
        out: dict[str, Answer] = {}
        for q in questions:
            out[q.id] = self._answer(q)
        return out

    def _answer(self, q: Question) -> Answer:
        raw = self.answers.get(q.id, _MISSING)
        if raw is _MISSING and "#" in q.id:
            raw = self.answers.get(q.id.split("#", 1)[0], _MISSING)
        if q.type is QuestionType.NOUL:
            value = self.default_noul if raw is _MISSING else float(raw)
            return Answer(q.id, QuestionType.NOUL, probability=value)
        if q.type is QuestionType.SCORE:
            level, conf = (0, self.default_confidence) if raw is _MISSING else raw
            return Answer(q.id, QuestionType.SCORE, score=float(level), confidence=float(conf))
        option, conf = (
            (q.options[0].id if q.options else "other", self.default_confidence)
            if raw is _MISSING
            else raw
        )
        return Answer(q.id, QuestionType.CHOICE, option=str(option), confidence=float(conf))


@dataclass
class ScriptedLLMClient:
    """Returns the next canned completion; raises when the script runs out."""

    completions: list[str] = field(default_factory=list)
    prompts: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        if not self.completions:
            raise AssertionError("ScriptedLLMClient ran out of completions")
        return self.completions.pop(0)


class _Missing:
    __slots__ = ()


_MISSING = _Missing()
