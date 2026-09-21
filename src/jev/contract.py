"""The plan contract: the single JSON object the compiler emits.

Parsing is deliberately tolerant — a malformed plan must still reach the lint,
which is where it is rejected with a named check (L1) rather than a traceback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .primitives import Question


class TaskShape(str, Enum):
    JUDGMENT = "judgment"
    GENERATIVE = "generative"
    MIXED = "mixed"
    EXTRACTION = "extraction"


class JevRole(str, Enum):
    NONE = "none"
    CORE = "core"
    PRE = "pre"
    POST = "post"
    PRE_POST = "pre+post"

    @property
    def has_pre(self) -> bool:
        return self in (JevRole.PRE, JevRole.PRE_POST)

    @property
    def has_post(self) -> bool:
        return self in (JevRole.POST, JevRole.PRE_POST)

    @property
    def is_core(self) -> bool:
        return self is JevRole.CORE


class PlanError(ValueError):
    """Raised when a payload cannot be read as a plan at all (L1 blocks)."""


@dataclass(frozen=True)
class State:
    id: str
    source: str
    content: Any
    pruned_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "content": self.content,
            "pruned_fields": list(self.pruned_fields),
        }


@dataclass(frozen=True)
class Round:
    round: int
    state: str
    questions: tuple[Question, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "state": self.state,
            "questions": [q.to_dict() for q in self.questions],
        }


@dataclass(frozen=True)
class PolicyRule:
    when: str
    then: str

    @property
    def escalates(self) -> bool:
        return "escalate" in self.then.lower() or "escalation" in self.then.lower()

    def to_dict(self) -> dict[str, str]:
        return {"when": self.when, "then": self.then}


@dataclass(frozen=True)
class PostCheck:
    id: str
    instructions: str
    type: str = "noul"
    state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type, "instructions": self.instructions}
        if self.state:
            out["state"] = self.state
        return out


@dataclass(frozen=True)
class Plan:
    task_shape: TaskShape
    jev_role: JevRole
    gate_rationale: str
    asks: tuple[str, ...] = ()
    states: dict[str, State] = field(default_factory=dict)
    rounds: tuple[Round, ...] = ()
    policy: tuple[PolicyRule, ...] = ()
    residual_prompt: str | None = None
    post_checks: tuple[PostCheck, ...] = ()
    #: Kept so the lint and the synthesis can compare the plan to what was asked.
    request: str = ""

    # -- questions ---------------------------------------------------------
    @property
    def questions(self) -> tuple[Question, ...]:
        return tuple(q for r in self.rounds for q in r.questions)

    def question(self, question_id: str) -> Question | None:
        for q in self.questions:
            if q.id == question_id:
                return q
        return None

    def state_of(self, question_id: str) -> State | None:
        for r in self.rounds:
            if any(q.id == question_id for q in r.questions):
                return self.states.get(r.state)
        return None

    # -- (de)serialisation -------------------------------------------------
    @classmethod
    def parse(cls, raw: str | dict[str, Any], request: str = "") -> "Plan":
        if isinstance(raw, str):
            try:
                raw = json.loads(_strip_code_fence(raw))
            except json.JSONDecodeError as exc:
                raise PlanError(f"plan is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise PlanError("plan must be a JSON object")
        try:
            shape = TaskShape(raw["task_shape"])
            role = JevRole(raw["jev_role"])
        except (KeyError, ValueError) as exc:
            raise PlanError(f"missing or unknown task_shape / jev_role: {exc}") from exc

        states: dict[str, State] = {}
        for sid, sraw in (raw.get("states") or {}).items():
            if not isinstance(sraw, dict):
                raise PlanError(f"state {sid} must be an object")
            states[sid] = State(
                id=sid,
                source=sraw.get("source", "user_input"),
                content=sraw.get("content"),
                pruned_fields=tuple(sraw.get("pruned_fields") or ()),
            )

        rounds: list[Round] = []
        for rraw in raw.get("rounds") or []:
            try:
                questions = tuple(Question.parse(q) for q in rraw.get("questions") or [])
            except (KeyError, ValueError) as exc:
                raise PlanError(f"malformed question: {exc}") from exc
            rounds.append(
                Round(
                    round=int(rraw.get("round", len(rounds) + 1)),
                    state=rraw.get("state", ""),
                    questions=questions,
                )
            )

        policy = tuple(
            PolicyRule(when=str(p.get("when", "")), then=str(p.get("then", "")))
            for p in raw.get("policy") or []
        )
        post_checks = tuple(
            PostCheck(
                id=str(p.get("id", f"v{i + 1}")),
                instructions=str(p.get("instructions", "")),
                type=str(p.get("type", "noul")),
                state=p.get("state"),
            )
            for i, p in enumerate(raw.get("post_checks") or [])
        )
        asks_raw = raw.get("asks") or []
        if isinstance(asks_raw, str):
            asks_raw = [asks_raw]

        return cls(
            task_shape=shape,
            jev_role=role,
            gate_rationale=str(raw.get("gate_rationale", "")),
            asks=tuple(str(a) for a in asks_raw),
            states=states,
            rounds=tuple(rounds),
            policy=policy,
            residual_prompt=raw.get("residual_prompt"),
            post_checks=post_checks,
            request=request,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_shape": self.task_shape.value,
            "jev_role": self.jev_role.value,
            "gate_rationale": self.gate_rationale,
            "asks": list(self.asks),
            "states": {sid: s.to_dict() for sid, s in self.states.items()},
            "rounds": [r.to_dict() for r in self.rounds],
            "policy": [p.to_dict() for p in self.policy],
            "residual_prompt": self.residual_prompt,
            "post_checks": [p.to_dict() for p in self.post_checks],
        }

    def to_json(self, **kw: Any) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)


def _strip_code_fence(text: str) -> str:
    """Tolerate a ```json fence around the compiler's output."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def passthrough_plan(request: str, shape: TaskShape, rationale: str = "") -> Plan:
    """The plan for ``jev_role = none``: three fields and the request verbatim."""
    return Plan(
        task_shape=shape,
        jev_role=JevRole.NONE,
        gate_rationale=rationale,
        residual_prompt=request,
        request=request,
    )
