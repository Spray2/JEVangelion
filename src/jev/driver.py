"""Driver mode: run the pipeline without letting it call any model itself.

Inside a Claude session the models are already there — Claude is the main LLM
and holds the MCP connection to JEV — so a second API client and a second MCP
connection would be duplicated work. Here the pipeline asks instead of calling:
it stops at every model call, hands out what to ask, and resumes once the answer
comes back.

The mechanism is replay. Every stage between two model calls is pure Python, so
the run is fully determined by the sequence of answers. The driver records the
answers and re-executes :func:`jev.pipeline.run` from the start each time,
feeding the recorded ones back; when the pipeline asks for one that is not yet
recorded, the replaying client raises :class:`Pending` and the driver reports
the call. No generator to freeze, no state machine to keep in sync — and the
transcript is a plain JSON file that survives between turns, processes and
machines.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from .gate import GATE_V2, GATE_V3, GateVariant
from .pipeline import PipelineResult, run
from .primitives import (
    JEV_MODEL_VERSION,
    Answer,
    ChoiceOption,
    Question,
    QuestionType,
    ScoreLevel,
)

GATES: dict[str, GateVariant] = {"v2": GATE_V2, "v3": GATE_V3}


class DriverError(RuntimeError):
    """The transcript does not match the run it claims to describe."""


@dataclass(frozen=True)
class Call:
    """A model call the pipeline needs answered before it can go on."""

    kind: Literal["jev", "llm"]
    index: int
    fingerprint: str
    purpose: str
    state: Any = None
    questions: list[dict[str, Any]] | None = None
    system: str | None = None
    user: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "index": self.index,
            "fingerprint": self.fingerprint,
            "purpose": self.purpose,
        }
        if self.kind == "jev":
            out["state"] = self.state
            out["questions"] = self.questions
            out["expects"] = _expected_answer_shape(self.questions or [])
        else:
            out["system"] = self.system
            out["user"] = self.user
            out["expects"] = "a string: the model's completion"
        return out


class Pending(Exception):
    """Raised by the replaying clients when the next answer is not recorded yet."""

    def __init__(self, call: Call) -> None:
        super().__init__(f"{call.kind} call #{call.index} ({call.purpose}) has no answer yet")
        self.call = call


# -- replaying clients -----------------------------------------------------


@dataclass
class _Replay:
    """Shared cursor over the recorded answers."""

    records: list[dict[str, Any]]
    cursor: int = 0
    strict: bool = True

    def take(self, kind: str, fingerprint: str, build: Any) -> Any:
        index = self.cursor
        self.cursor += 1
        if index >= len(self.records):
            raise Pending(build(index, fingerprint))
        record = self.records[index]
        if record.get("kind") != kind:
            raise DriverError(
                f"record #{index} is a {record.get('kind')!r} answer but the pipeline "
                f"is making a {kind!r} call — the transcript belongs to another run"
            )
        if self.strict and record.get("fingerprint") not in (None, fingerprint):
            raise DriverError(
                f"record #{index} answers a different call "
                f"({record['fingerprint']} != {fingerprint}); the request or the inputs "
                f"changed since it was recorded"
            )
        return record["result"]


@dataclass
class ReplayingJevClient:
    replay: _Replay
    model_version: str = JEV_MODEL_VERSION

    def ask(self, state: Any, questions: Sequence[Question]) -> dict[str, Answer]:
        payload = [q.to_dict() for q in questions]
        fingerprint = _fingerprint({"state": state, "questions": payload})
        result = self.replay.take(
            "jev",
            fingerprint,
            lambda i, fp: Call("jev", i, fp, _purpose(questions), state=state,
                               questions=payload),
        )
        return parse_answers(questions, result)


@dataclass
class ReplayingLLMClient:
    replay: _Replay

    def complete(self, system: str, user: str) -> str:
        fingerprint = _fingerprint({"system": system, "user": user})
        result = self.replay.take(
            "llm",
            fingerprint,
            lambda i, fp: Call("llm", i, fp, _llm_purpose(system), system=system, user=user),
        )
        if not isinstance(result, str):
            raise DriverError(f"an llm answer must be a string, got {type(result).__name__}")
        return result


# -- answer parsing --------------------------------------------------------


def parse_answers(
    questions: Sequence[Question], payload: Any
) -> dict[str, Answer]:
    """Turn whatever the JEV tool returned into typed answers.

    Deliberately tolerant, because the wire shape depends on the MCP server in
    front of JEV. All of these work, per question id:

        {"q1": 0.87}
        {"q1": {"probability": 0.87}}
        {"q2": {"score": 2.17, "confidence": 0.88}}
        {"q3": {"option": "price", "confidence": 0.91}}
        [{"id": "q1", "probability": 0.87}, ...]

    ``score`` may be fractional: JEV returns an expected level, and the gate's
    ``cost`` threshold (1.75) compares against it.
    """
    by_id = _index_payload(payload)
    out: dict[str, Answer] = {}
    for q in questions:
        raw = by_id.get(q.id)
        if raw is None:
            raise DriverError(
                f"no answer for question {q.id!r} (answers given: {sorted(by_id)})"
            )
        out[q.id] = _parse_one(q, raw)
    return out


def _index_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, Mapping):
        # A server that wraps its answers, e.g. {"answers": {...}}.
        for wrapper in ("answers", "results", "questions"):
            inner = payload.get(wrapper)
            # typesafe-jev puts model/usage/meta next to its answers.
            if inner is not None and set(payload) - {wrapper} <= {"model", "usage", "meta"}:
                return _index_payload(inner)
        return dict(payload)
    if isinstance(payload, list):
        indexed: dict[str, Any] = {}
        for entry in payload:
            if not isinstance(entry, Mapping):
                raise DriverError("a list of answers must hold objects with an 'id'")
            key = entry.get("id") or entry.get("question_id") or entry.get("question")
            if key is None:
                raise DriverError(f"answer without an id: {entry!r}")
            indexed[str(key)] = entry
        return indexed
    raise DriverError(f"cannot read answers of type {type(payload).__name__}")


def _parse_one(q: Question, raw: Any) -> Answer:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if q.type is QuestionType.NOUL:
            return Answer(q.id, QuestionType.NOUL, probability=value)
        if q.type is QuestionType.SCORE:
            return Answer(q.id, QuestionType.SCORE, score=value, confidence=None)
        raise DriverError(f"{q.id}: a choice needs an option, got a number")
    if isinstance(raw, bool):
        if q.type is not QuestionType.NOUL:
            raise DriverError(f"{q.id}: a boolean only answers a noul")
        return Answer(q.id, QuestionType.NOUL, probability=1.0 if raw else 0.0)
    if isinstance(raw, str):
        if q.type is not QuestionType.CHOICE:
            raise DriverError(f"{q.id}: a bare string only answers a choice")
        return Answer(q.id, QuestionType.CHOICE, option=raw, confidence=None)
    if not isinstance(raw, Mapping):
        raise DriverError(f"{q.id}: cannot read an answer of type {type(raw).__name__}")

    confidence = _first_float(raw, "confidence", "conf", "certainty")
    if q.type is QuestionType.NOUL:
        probability = _first_float(raw, "probability", "noul", "p", "value", "score", "yes")
        if probability is None:
            raise DriverError(f"{q.id}: a noul answer needs a probability")
        return Answer(q.id, QuestionType.NOUL, probability=probability)
    if q.type is QuestionType.SCORE:
        level = _first_float(raw, "score", "level", "expected", "value")
        if level is None:
            raise DriverError(f"{q.id}: a score answer needs a level")
        return Answer(q.id, QuestionType.SCORE, score=level, confidence=confidence)
    option = _first_str(raw, "option", "choice", "id", "value", "label")
    if option is None:
        raise DriverError(f"{q.id}: a choice answer needs an option")
    return Answer(q.id, QuestionType.CHOICE, option=option, confidence=confidence)


def _first_float(raw: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _first_str(raw: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# -- the driver ------------------------------------------------------------


@dataclass
class Transcript:
    """Everything needed to resume a run: the request, the input, the answers."""

    request: str
    inputs: dict[str, Any] = field(default_factory=dict)
    source_text: str | None = None
    gate: str = "v2"
    compiler_version: str = "v0.4"
    #: False runs only the deterministic lint checks, saving one JEV call per
    #: distinct check state (eight on a pre+post plan).
    lint_with_jev: bool = True
    records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "inputs": self.inputs,
            "source_text": self.source_text,
            "gate": self.gate,
            "compiler_version": self.compiler_version,
            "lint_with_jev": self.lint_with_jev,
            "records": self.records,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Transcript":
        if "request" not in raw:
            raise DriverError("a transcript needs a 'request'")
        return cls(
            request=str(raw["request"]),
            inputs=dict(raw.get("inputs") or {}),
            source_text=raw.get("source_text"),
            gate=str(raw.get("gate", "v2")),
            compiler_version=str(raw.get("compiler_version", "v0.4")),
            lint_with_jev=bool(raw.get("lint_with_jev", True)),
            records=list(raw.get("records") or []),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Transcript":
        return cls.from_dict(json.loads(text))


@dataclass
class Driver:
    """Replays a transcript and reports the next model call, or the result."""

    transcript: Transcript
    strict: bool = True

    def step(self) -> Call | PipelineResult:
        """The next call to make, or the finished run."""
        replay = _Replay(self.transcript.records, strict=self.strict)
        jev = ReplayingJevClient(replay)
        llm = ReplayingLLMClient(replay)
        try:
            return run(
                self.transcript.request,
                jev=jev,
                llm=llm,
                inputs=self.transcript.inputs,
                source_text=self.transcript.source_text,
                gate_variant=GATES[self.transcript.gate],
                compiler_version=self.transcript.compiler_version,
                lint_with_jev=self.transcript.lint_with_jev,
            )
        except Pending as pending:
            return pending.call

    def submit(self, result: Any, fingerprint: str | None = None) -> Call | PipelineResult:
        """Record one answer and report what comes next."""
        pending = self.step()
        if isinstance(pending, PipelineResult):
            raise DriverError("the run is already finished; nothing is pending")
        if fingerprint is not None and fingerprint != pending.fingerprint:
            raise DriverError(
                f"this answer is for call {fingerprint}, but {pending.fingerprint} is pending"
            )
        self.transcript.records.append(
            {"kind": pending.kind, "fingerprint": pending.fingerprint,
             "purpose": pending.purpose, "result": result}
        )
        return self.step()

    def rewind(self, count: int = 1) -> None:
        """Drop the last answers, to redo a call that went wrong."""
        if count < 1:
            raise DriverError("rewind takes a positive count")
        del self.transcript.records[-count:]


# -- labels and fingerprints ----------------------------------------------


def _purpose(questions: Sequence[Question]) -> str:
    ids = {q.id for q in questions}
    if "shape" in ids:
        return "gate"
    if any(i.startswith("L") and "#" in i for i in ids):
        checks = sorted({i.split("#", 1)[0] for i in ids})
        return f"lint {', '.join(checks)}"
    if all(i.startswith("d") for i in ids):
        return "consistency between the decisions and the generated text"
    if all(i.startswith("v") for i in ids):
        return "post checks"
    return "round"


def _llm_purpose(system: str) -> str:
    return "compile the plan" if system else "generate the text"


def _fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _expected_answer_shape(questions: Sequence[dict[str, Any]]) -> dict[str, str]:
    shapes = {
        "noul": "a probability in [0,1] — \"yes\" is the high value, no confidence",
        "score": "{\"score\": <expected level, may be fractional>, \"confidence\": <0-1>}",
        "choice": "{\"option\": \"<option id>\", \"confidence\": <0-1>}",
    }
    return {q["id"]: shapes[q["type"]] for q in questions}


# -- probing a real JEV server --------------------------------------------

#: A minimal call covering all three primitives at once. Sending this to the
#: real server, once, is what tells you the wire shape its answers come in —
#: cheaper and clearer than discovering it halfway through a run.
PROBE_STATE = {"text": "Se il prezzo non cambia entro venerdì disdiciamo il contratto."}

PROBE_QUESTIONS: tuple[Question, ...] = (
    Question(
        id="p1",
        type=QuestionType.NOUL,
        instructions="Does `text` mention a deadline?",
    ),
    Question(
        id="p2",
        type=QuestionType.SCORE,
        instructions="How firmly does `text` express an intention to leave?",
        criteria=(
            ScoreLevel("No mention of leaving", ("not happy with the last delivery",)),
            ScoreLevel("Leaving mentioned as a possibility",
                       ("we might look at other options",)),
            ScoreLevel("Conditional ultimatum",
                       ("if this is not fixed by Friday we will cancel",)),
            ScoreLevel("Formal notice of cancellation",
                       ("please consider this our notice of termination",)),
        ),
    ),
    Question(
        id="p3",
        type=QuestionType.CHOICE,
        instructions="What is the main reason for dissatisfaction in `text`?",
        criteria=(
            ChoiceOption("price", "The cost of the service or product"),
            ChoiceOption("service", "How the customer was treated"),
            ChoiceOption("other", "Anything else"),
        ),
    ),
)


@dataclass(frozen=True)
class ProbeReading:
    """What the parser made of a real answer, and what looks off."""

    answers: dict[str, Answer]
    warnings: list[str]
    notes: list[str]


def probe_call() -> dict[str, Any]:
    return {
        "state": PROBE_STATE,
        "questions": [q.to_dict() for q in PROBE_QUESTIONS],
        "expects": _expected_answer_shape([q.to_dict() for q in PROBE_QUESTIONS]),
    }


def read_probe(payload: Any) -> ProbeReading:
    """Parse a real answer to :func:`probe_call` and report what is missing.

    Everything here is about the wire shape, not about whether JEV judged well.
    The one exception is the polarity note on the Noul: "yes" must be the high
    value, and a server that inverts it would break every plan silently.
    """
    answers = parse_answers(PROBE_QUESTIONS, payload)
    warnings: list[str] = []
    notes: list[str] = []

    noul = answers["p1"]
    if noul.probability is not None and noul.probability < 0.5:
        warnings.append(
            "p1 came back below 0.5, but `text` does mention a deadline. Either the "
            "server inverts the polarity of a Noul, or the answer was read from the "
            "wrong field — every plan assumes \"yes\" is the high value."
        )

    score = answers["p2"]
    if score.score is not None and float(score.score).is_integer():
        warnings.append(
            "p2 came back as a whole number. If the server only ever returns the "
            "discrete level, gate v3 loses resolution: its `cost` threshold is 1.75 and "
            "compares against the expected (fractional) level. Gate v2 is unaffected."
        )
    if score.confidence is None:
        warnings.append(
            "p2 came back without a confidence. The mandatory escalation rule is "
            "`any.confidence < 0.5`, so without it a plan of Scores never escalates."
        )

    choice = answers["p3"]
    if choice.option not in {o.id for o in PROBE_QUESTIONS[2].options}:
        warnings.append(
            f"p3 answered {choice.option!r}, which is not one of the option ids "
            f"({', '.join(o.id for o in PROBE_QUESTIONS[2].options)}). The policy "
            f"compares option ids literally."
        )
    if choice.confidence is None:
        warnings.append(
            "p3 came back without a confidence. The gate reads it to decide whether to "
            "escalate on an unsure shape."
        )

    notes.append(f"noul  p1 -> probability {noul.probability}")
    notes.append(f"score p2 -> level {score.score} (discrete {score.level}), "
                 f"confidence {score.confidence}")
    notes.append(f"choice p3 -> option {choice.option!r}, confidence {choice.confidence}")
    return ProbeReading(answers=answers, warnings=warnings, notes=notes)
