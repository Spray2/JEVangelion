"""The round runtime: one state, many questions, once the input finally arrives.

The compiler wrote placeholders; here they are bound to real data. Template
questions are expanded — ``for_each`` gives one state per element, so the
questions stay atomic, and ``criteria_from`` instantiates one question per
criterion — and every group of questions sharing a state costs a single
``jev_ask``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from .client import JevClient
from .contract import Plan, Round, State
from .policy import Scope, count_above
from .primitives import Answer, Question, QuestionType

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z_0-9]*)\s*\}\}")


class MissingInput(KeyError):
    """A placeholder in the plan has no value in the runtime input."""


def placeholder_name(text: Any) -> str | None:
    """The single placeholder a string consists of, if that is all it is."""
    if not isinstance(text, str):
        return None
    m = PLACEHOLDER.fullmatch(text.strip())
    return m.group(1) if m else None


def resolve(content: Any, inputs: Mapping[str, Any]) -> Any:
    """Bind ``{{name}}`` placeholders to runtime values, at any depth."""
    name = placeholder_name(content)
    if name is not None:
        if name not in inputs:
            raise MissingInput(name)
        return inputs[name]
    if isinstance(content, str):
        def sub(m: re.Match[str]) -> str:
            key = m.group(1)
            if key not in inputs:
                raise MissingInput(key)
            return str(inputs[key])
        return PLACEHOLDER.sub(sub, content)
    if isinstance(content, dict):
        return {k: resolve(v, inputs) for k, v in content.items()}
    if isinstance(content, (list, tuple)):
        return [resolve(v, inputs) for v in content]
    return content


# -- expansion -------------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    """One concrete question, ready for JEV."""

    question: Question
    state: Any
    base_id: str
    item_key: str | None = None
    item_index: int | None = None


def expand(round_: Round, states: Mapping[str, State], inputs: Mapping[str, Any]) -> list[Instance]:
    base_state_def = states.get(round_.state)
    base_state = resolve(base_state_def.content, inputs) if base_state_def else {}
    instances: list[Instance] = []
    for q in round_.questions:
        if q.for_each:
            instances.extend(_expand_for_each(q, base_state, inputs))
        elif q.criteria_from:
            instances.extend(_expand_criteria_from(q, base_state, inputs))
        else:
            instances.append(Instance(q, base_state, q.id))
    return instances


def _expand_for_each(q: Question, base_state: Any, inputs: Mapping[str, Any]) -> list[Instance]:
    name = placeholder_name(q.for_each) or str(q.for_each)
    if name not in inputs:
        raise MissingInput(name)
    collection = inputs[name]
    out: list[Instance] = []
    for i, element in enumerate(_enumerate(collection)):
        key = _item_key(element, i)
        state = _state_without(base_state, name)
        state = {**state, "item": element} if isinstance(state, dict) else {"item": element}
        out.append(Instance(replace(q, id=f"{q.id}#{key}"), state, q.id, key, i))
    return out


def _expand_criteria_from(
    q: Question, base_state: Any, inputs: Mapping[str, Any]
) -> list[Instance]:
    name = placeholder_name(q.criteria_from) or str(q.criteria_from)
    if name not in inputs:
        raise MissingInput(name)
    criteria = inputs[name]
    out: list[Instance] = []
    for i, criterion in enumerate(_enumerate(criteria)):
        key = _item_key(criterion, i)
        state = base_state if isinstance(base_state, dict) else {"content": base_state}
        out.append(
            Instance(replace(q, id=f"{q.id}#{key}"), {**state, "criterion": criterion},
                     q.id, key, i)
        )
    return out


def _enumerate(collection: Any) -> list[Any]:
    if isinstance(collection, Mapping):
        return list(collection.values())
    if isinstance(collection, (list, tuple)):
        return list(collection)
    raise TypeError(f"expected a collection, got {type(collection).__name__}")


def _item_key(element: Any, index: int) -> str:
    if isinstance(element, Mapping):
        for field_name in ("id", "key", "name"):
            if field_name in element:
                return str(element[field_name])
    if isinstance(element, str) and len(element) <= 40:
        return element
    return str(index)


def _state_without(state: Any, key: str) -> Any:
    if isinstance(state, dict):
        return {k: v for k, v in state.items() if k != key}
    return {}


# -- execution -------------------------------------------------------------


@dataclass
class RunResult:
    """Every answer of every round, plus the scopes the policy is run against."""

    answers: dict[str, Answer] = field(default_factory=dict)
    instances: list[Instance] = field(default_factory=list)
    calls: int = 0

    @property
    def scalar(self) -> dict[str, Answer]:
        return {
            inst.base_id: self.answers[inst.question.id]
            for inst in self.instances
            if inst.item_key is None and inst.question.id in self.answers
        }

    @property
    def item_keys(self) -> list[str]:
        seen: list[str] = []
        for inst in self.instances:
            if inst.item_key is not None and inst.item_key not in seen:
                seen.append(inst.item_key)
        return seen

    def for_item(self, item_key: str) -> dict[str, Answer]:
        return {
            inst.base_id: self.answers[inst.question.id]
            for inst in self.instances
            if inst.item_key == item_key and inst.question.id in self.answers
        }

    @property
    def uncertain(self) -> list[Answer]:
        return [a for a in self.answers.values() if a.is_uncertain]

    def counts(self) -> dict[str, int]:
        """Counts are computed here, never asked of JEV."""
        grouped: dict[str, list[Answer]] = {}
        for inst in self.instances:
            if inst.item_key is None:
                continue
            answer = self.answers.get(inst.question.id)
            if answer is not None:
                grouped.setdefault(inst.base_id, []).append(answer)
        return {qid: count_above(answers) for qid, answers in grouped.items()}

    def scopes(self) -> list[Scope]:
        counts = self.counts()
        scalar = self.scalar
        scopes = [Scope(scalar, counts=counts)]
        for key in self.item_keys:
            merged = {**scalar, **self.for_item(key)}
            index = next(
                (i.item_index for i in self.instances if i.item_key == key), None
            )
            scopes.append(Scope(merged, item_key=key, item_index=index, counts=counts))
        return scopes


def run_rounds(jev: JevClient, plan: Plan, inputs: Mapping[str, Any]) -> RunResult:
    """Execute the plan's rounds in order. Dependent questions live in later
    rounds, so ordering here is all the dependency handling that is needed."""
    result = RunResult()
    for round_ in sorted(plan.rounds, key=lambda r: r.round):
        instances = expand(round_, plan.states, inputs)
        result.instances.extend(instances)
        for state, group in _group_by_state(instances):
            answers = jev.ask(state, [inst.question for inst in group])
            result.calls += 1
            for inst in group:
                answer = answers.get(inst.question.id)
                if answer is None:
                    continue
                result.answers[inst.question.id] = replace(
                    answer, item_key=inst.item_key, item_index=inst.item_index
                )
    return result


def _group_by_state(instances: Sequence[Instance]) -> list[tuple[Any, list[Instance]]]:
    groups: list[tuple[int, Any, list[Instance]]] = []
    for inst in instances:
        for entry in groups:
            if entry[0] == id(inst.state) or entry[1] is inst.state or entry[1] == inst.state:
                entry[2].append(inst)
                break
        else:
            groups.append((id(inst.state), inst.state, [inst]))
    return [(state, group) for _, state, group in groups]


def resolved_facts(result: RunResult, plan: Plan) -> dict[str, str]:
    """The ``{{qN}}`` substitutions injected into the residual prompt.

    A decision reaches the main LLM as a fact, not as a question to re-open.
    """
    facts: dict[str, str] = {}
    for base_id, answer in result.scalar.items():
        facts[base_id] = render_answer(answer, plan.question(base_id))
    for base_id in {inst.base_id for inst in result.instances if inst.item_key is not None}:
        parts = []
        for key in result.item_keys:
            answer = result.for_item(key).get(base_id)
            if answer is not None:
                parts.append(f"{key}: {render_answer(answer, plan.question(base_id))}")
        facts[base_id] = "; ".join(parts)
    return facts


def render_answer(answer: Answer, question: Question | None) -> str:
    if answer.type is QuestionType.NOUL:
        p = answer.probability or 0.0
        if answer.is_uncertain:
            return f"uncertain ({p:.2f})"
        return f"yes ({p:.2f})" if p >= 0.5 else f"no ({p:.2f})"
    if answer.type is QuestionType.SCORE:
        levels = question.levels if question else ()
        level = answer.level
        label = levels[level].what if levels and level is not None and level < len(levels) else ""
        return f"level {answer.score:.2f}{' - ' + label if label else ''}"
    return str(answer.option)


def fill_residual_prompt(plan: Plan, facts: Mapping[str, str]) -> str:
    """Substitute the resolved outcomes into the residual prompt."""
    if plan.residual_prompt is None:
        return ""
    def sub(m: re.Match[str]) -> str:
        return str(facts.get(m.group(1), m.group(0)))
    return PLACEHOLDER.sub(sub, plan.residual_prompt)
