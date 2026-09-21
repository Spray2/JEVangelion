"""The policy stage: declarative rules over question ids, executed in code.

Thresholds and aggregations live here and nowhere else — never inside a
question — so recalibration never touches a prompt. The grammar is deliberately
small:

    when := term (("AND" | "OR") term)*
    term := "(" when ")" | operand OP value
    operand := qN | qN.score | qN.confidence | qN.probability | qN.choice
             | any.confidence | any.uncertain | count.<qid>
    OP := >= | > | <= | < | == | !=

``qN`` on its own resolves to the Noul probability, the Score expected level or
the Choice option id. ``any.confidence`` ranges over the answers that carry a
confidence (Noul has none); ``any.uncertain`` is true when any answer sits in
the band the runtime flags for review. ``count.<qid>`` is the number of items of
a template question whose answer is above 0.5 — a count JEV is never asked for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .contract import PolicyRule
from .primitives import CONFIDENCE_FLOOR, Answer, QuestionType


class PolicyError(ValueError):
    """The rule could not be parsed or refers to a question that does not exist."""


# -- scope ----------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """The answers a rule is evaluated against.

    A plan has one global scope (the scalar questions) plus one scope per item
    of a template question, where ``item_key`` names the element.
    """

    answers: Mapping[str, Answer]
    item_key: str | None = None
    item_index: int | None = None
    counts: Mapping[str, int] = field(default_factory=dict)

    def resolve(self, operand: str) -> Any:
        head, _, attr = operand.partition(".")
        if head == "any":
            return self._any(attr)
        if head == "count":
            if attr not in self.counts:
                raise PolicyError(f"no count available for {attr!r}")
            return self.counts[attr]
        answer = self.answers.get(head)
        if answer is None:
            raise PolicyError(f"rule refers to unknown question {head!r}")
        if not attr:
            return answer.value
        if attr == "score":
            return answer.score
        if attr == "probability":
            return answer.probability
        if attr == "confidence":
            return answer.confidence
        if attr in ("choice", "option"):
            return answer.option
        if attr == "uncertain":
            return answer.is_uncertain
        raise PolicyError(f"unknown attribute {attr!r} on {head!r}")

    def _any(self, attr: str) -> Any:
        if attr == "confidence":
            values = [a.confidence for a in self.answers.values() if a.confidence is not None]
            # No confidence anywhere: nothing can fall below the floor.
            return min(values) if values else 1.0
        if attr == "uncertain":
            return any(a.is_uncertain for a in self.answers.values())
        raise PolicyError(f"unknown aggregate any.{attr}")


# -- expression evaluation -------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<op>>=|<=|==|!=|>|<)
      | (?P<bool>\bAND\b|\bOR\b|\band\b|\bor\b)
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<string>"[^"]*"|'[^']*')
      | (?P<ident>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)
    )""",
    re.X,
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise PolicyError(f"cannot parse {expr!r} at offset {pos}")
        kind = m.lastgroup or ""
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: Sequence[tuple[str, str]], expr: str) -> None:
        self.tokens = list(tokens)
        self.pos = 0
        self.expr = expr

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise PolicyError(f"unexpected end of rule {self.expr!r}")
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self, scope: Scope) -> bool:
        value = self.parse_or(scope)
        if self.peek() is not None:
            raise PolicyError(f"trailing tokens in rule {self.expr!r}")
        return value

    def parse_or(self, scope: Scope) -> bool:
        value = self.parse_and(scope)
        while (tok := self.peek()) and tok[0] == "bool" and tok[1].lower() == "or":
            self.take()
            value = self.parse_and(scope) or value
        return value

    def parse_and(self, scope: Scope) -> bool:
        value = self.parse_term(scope)
        while (tok := self.peek()) and tok[0] == "bool" and tok[1].lower() == "and":
            self.take()
            value = self.parse_term(scope) and value
        return value

    def parse_term(self, scope: Scope) -> bool:
        kind, text = self.take()
        if kind == "lparen":
            value = self.parse_or(scope)
            close = self.take()
            if close[0] != "rparen":
                raise PolicyError(f"unbalanced parentheses in {self.expr!r}")
            return value
        if kind != "ident":
            raise PolicyError(f"expected a question reference in {self.expr!r}, got {text!r}")
        nxt = self.peek()
        if nxt is None or nxt[0] != "op":
            # A bare boolean operand, e.g. "any.uncertain".
            return bool(scope.resolve(text))
        self.take()
        op = nxt[1]
        rhs_kind, rhs_text = self.take()
        if rhs_kind == "number":
            rhs: Any = float(rhs_text)
        elif rhs_kind == "string":
            rhs = rhs_text[1:-1]
        elif rhs_kind == "ident":
            rhs = rhs_text
        else:
            raise PolicyError(f"expected a value in {self.expr!r}, got {rhs_text!r}")
        return _compare(scope.resolve(text), op, rhs, self.expr)


def _compare(left: Any, op: str, right: Any, expr: str) -> bool:
    if left is None:
        # An unanswered question cannot satisfy a rule; it never blocks one either.
        return False
    if isinstance(left, str) or isinstance(right, str):
        if op == "==":
            return str(left) == str(right)
        if op == "!=":
            return str(left) != str(right)
        raise PolicyError(f"cannot order strings in {expr!r}")
    lhs, rhs = float(left), float(right)
    return {
        ">=": lhs >= rhs,
        ">": lhs > rhs,
        "<=": lhs <= rhs,
        "<": lhs < rhs,
        "==": lhs == rhs,
        "!=": lhs != rhs,
    }[op]


def evaluate(expression: str, scope: Scope) -> bool:
    if not expression.strip():
        raise PolicyError("empty rule")
    return _Parser(_tokenize(expression), expression).parse(scope)


# -- running a policy ------------------------------------------------------


@dataclass(frozen=True)
class TriggeredRule:
    rule: PolicyRule
    item_key: str | None = None

    @property
    def escalates(self) -> bool:
        return self.rule.escalates


@dataclass
class PolicyOutcome:
    triggered: list[TriggeredRule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        return any(t.escalates for t in self.triggered)

    @property
    def actions(self) -> list[str]:
        return [t.rule.then for t in self.triggered]

    def for_item(self, item_key: str | None) -> list[TriggeredRule]:
        return [t for t in self.triggered if t.item_key == item_key]


def run_policy(rules: Iterable[PolicyRule], scopes: Sequence[Scope]) -> PolicyOutcome:
    """Evaluate every rule in every scope.

    A rule that names a question absent from one scope simply does not apply
    there: a rule over a ``for_each`` question is meaningless in the global
    scope and meaningful in every item scope. Only a rule that fails in *every*
    scope is reported as an error — and reported, never raised, so one broken
    rule cannot swallow the round.
    """
    outcome = PolicyOutcome()
    for rule in rules:
        errors: list[str] = []
        evaluated = False
        for scope in scopes:
            try:
                fired = evaluate(rule.when, scope)
            except PolicyError as exc:
                errors.append(f"{rule.when!r}: {exc}")
                continue
            evaluated = True
            if fired:
                outcome.triggered.append(TriggeredRule(rule, scope.item_key))
        if not evaluated and errors:
            outcome.errors.append(errors[0])
    return outcome


def count_above(answers: Iterable[Answer], threshold: float = CONFIDENCE_FLOOR) -> int:
    """Counts and weighted sums are computed here, never asked of JEV."""
    total = 0
    for a in answers:
        if a.type is QuestionType.NOUL and (a.probability or 0.0) >= threshold:
            total += 1
        elif a.type is QuestionType.SCORE and (a.score or 0.0) >= threshold:
            total += 1
    return total
