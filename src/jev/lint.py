"""The plan lint: L1-L13, run between the compiler and the rounds.

Five checks are deterministic and belong in code; L10 was moved there too, as
docs/spec.md recommends, by comparing the criteria named in the request with the
use of ``criteria_from``. The rest need JEV.

Policy: a failed code check always blocks. A JEV check below 0.5 sends the plan
back to the compiler once, carrying the failed check as feedback; on the second
failure the plan proceeds with a warning.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Sequence

from .client import JevClient
from .contract import JevRole, Plan
from .primitives import Question, QuestionRole, QuestionType

#: A JEV lint answer at or above this passes.
LINT_THRESHOLD = 0.5

#: Comparators and bare numbers in an instruction mean a threshold was baked
#: into a question instead of living in the policy.
_THRESHOLD_PATTERN = re.compile(r"(>=|<=|==|>|<|\b\d+(?:[.,]\d+)?\s*%?\b)")
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")

#: "leggibilità, test, performance e sicurezza" — three or more comma-separated
#: short noun phrases closed by a conjunction. When the request enumerates the
#: criteria like this, they are named at compile time and `criteria_from` is wrong.
_ENUMERATION = re.compile(
    r"(?:\b[\w][^,;:.!?]{1,40},\s*){2,}[^,;:.!?]{0,40}?\b(?:e|ed|and|o|oppure|or)\b\s+"
    r"[\w][^,;:.!?]{1,40}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CheckResult:
    id: str
    name: str
    where: str  # "code" or "jev"
    passed: bool
    detail: str = ""
    score: float | None = None
    target: str | None = None

    @property
    def blocking(self) -> bool:
        return self.where == "code" and not self.passed

    def as_feedback(self) -> str:
        target = f" ({self.target})" if self.target else ""
        score = f" [{self.score:.2f}]" if self.score is not None else ""
        return f"{self.id} {self.name}{target}{score}: {self.detail}"


@dataclass
class LintReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    @property
    def blocking_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.blocking]

    @property
    def advisory_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and not c.blocking]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_failures)

    def feedback(self) -> list[str]:
        return [c.as_feedback() for c in self.failures]

    def __iter__(self):  # pragma: no cover - convenience
        return iter(self.checks)


# -- entry point -----------------------------------------------------------


def lint_plan(plan: Plan, jev: JevClient | None = None) -> LintReport:
    """Run every check available. Without a JEV client only the code checks run."""
    report = LintReport()
    report.checks.extend(code_checks(plan))
    if jev is not None:
        report.checks.extend(jev_checks(plan, jev))
    return report


def code_checks(plan: Plan) -> list[CheckResult]:
    return [
        _l1_schema(plan),
        _l2_no_thresholds(plan),
        _l3_scores_anchored(plan),
        _l4_escalation(plan),
        *_l5_transmitted_in_code(plan),
        _l10_named_criteria(plan),
    ]


# -- L1: schema ------------------------------------------------------------


def _l1_schema(plan: Plan) -> CheckResult:
    problems: list[str] = []
    if not plan.asks:
        problems.append("asks is empty (L5/L6 have nothing to check)")

    seen: set[str] = set()
    for q in plan.questions:
        if q.id in seen:
            problems.append(f"duplicate question id {q.id!r}")
        seen.add(q.id)
        if not q.instructions.strip():
            problems.append(f"{q.id}: empty instructions")
        if q.type is QuestionType.NOUL and q.criteria:
            problems.append(f"{q.id}: a noul must have criteria null")
        if q.type is QuestionType.SCORE and len(q.levels) < 2:
            problems.append(f"{q.id}: a score needs at least two levels")
        if q.type is QuestionType.CHOICE and len(q.options) < 2:
            problems.append(f"{q.id}: a choice needs at least two options")
        if q.role is QuestionRole.POST:
            problems.append(f"{q.id}: verification never goes in rounds, only in post_checks")

    for r in plan.rounds:
        if r.state not in plan.states:
            problems.append(f"round {r.round}: unknown state {r.state!r}")
    for sid, state in plan.states.items():
        if state.source not in ("user_input", "llm_output"):
            problems.append(f"state {sid}: unknown source {state.source!r}")

    if plan.jev_role.is_core:
        if plan.residual_prompt is not None:
            problems.append("residual_prompt must be null when jev_role is core")
    elif plan.jev_role is not JevRole.NONE and not (plan.residual_prompt or "").strip():
        problems.append(f"residual_prompt is required for jev_role {plan.jev_role.value}")

    if len(plan.post_checks) > 4:
        problems.append(f"{len(plan.post_checks)} post checks, at most 4 allowed")
    for check in plan.post_checks:
        if check.type != "noul":
            problems.append(f"post check {check.id}: must be a noul")
    if plan.jev_role.has_post and not plan.post_checks:
        problems.append("a post role needs at least one post check")
    if not plan.jev_role.has_post and plan.post_checks:
        problems.append("post checks on a plan without a post role")
    if plan.jev_role.has_pre and not plan.questions:
        problems.append("a pre role needs at least one question")

    return CheckResult(
        "L1", "schema", "code", not problems,
        detail="; ".join(problems) if problems else "valid against the contract",
    )


# -- L2: no thresholds in the questions ------------------------------------


def _l2_no_thresholds(plan: Plan) -> CheckResult:
    offenders: list[str] = []
    for q in plan.questions:
        text = _PLACEHOLDER.sub(" ", q.instructions)
        found = _THRESHOLD_PATTERN.findall(text)
        if found:
            offenders.append(f"{q.id}: {', '.join(sorted({f.strip() for f in found}))}")
    return CheckResult(
        "L2", "no thresholds in questions", "code", not offenders,
        detail=("thresholds belong in the policy: " + "; ".join(offenders)) if offenders
        else "no comparator or number in any instruction",
    )


# -- L3: scores anchored ---------------------------------------------------


def _l3_scores_anchored(plan: Plan) -> CheckResult:
    offenders: list[str] = []
    for q in plan.questions:
        if q.type is not QuestionType.SCORE:
            continue
        for i, level in enumerate(q.levels):
            if not level.what.strip():
                offenders.append(f"{q.id} level {i}: no 'what'")
            if not level.examples:
                offenders.append(f"{q.id} level {i}: no examples")
    return CheckResult(
        "L3", "scores anchored", "code", not offenders,
        detail="; ".join(offenders) if offenders else "every level has 'what' and examples",
    )


# -- L4: escalation --------------------------------------------------------

_CONFIDENCE_RULE = re.compile(r"confidence\s*<\s*0?\.5", re.IGNORECASE)


def _l4_escalation(plan: Plan) -> CheckResult:
    escalating = [r for r in plan.policy if r.escalates]
    if plan.questions:
        ok = any(_CONFIDENCE_RULE.search(r.when) for r in escalating)
        detail = ("a rule escalates on confidence < 0.5" if ok
                  else "no escalation rule on confidence < 0.5")
    elif plan.jev_role.has_post:
        # Post-only plans: a post check still failing after one regeneration escalates.
        ok = bool(escalating)
        detail = ("a post-only plan escalates after a failed regeneration" if ok
                  else "post-only plan without an escalation rule")
    else:
        ok = True
        detail = "no questions and no post role: nothing to escalate"
    return CheckResult("L4", "escalation present", "code", ok, detail=detail)


# -- L5: asks transmitted (code fast path) ---------------------------------


def _l5_transmitted_in_code(plan: Plan) -> list[CheckResult]:
    """Verbatim residual prompt settles L5 without JEV; that is why it is here.

    The JEV form of L5 misreads literal fragments (docs/spec.md: "per la
    pubblicazione sul sito" scored 0.10 while appearing word for word), so code
    decides whenever it can and :func:`jev_checks` only handles the rest.
    """
    if plan.jev_role.is_core:
        return [CheckResult("L5", "asks transmitted", "code", True,
                            detail="core plan: no residual prompt to carry the asks")]
    if _same_text(plan.residual_prompt, plan.request):
        return [CheckResult("L5", "asks transmitted", "code", True,
                            detail="residual prompt is the request verbatim")]
    return []  # decided by JEV


# -- L10: a runtime construct on criteria the request already names --------


def _l10_named_criteria(plan: Plan) -> CheckResult:
    templated = [q for q in plan.questions if q.criteria_from]
    if not templated:
        return CheckResult("L10", "criteria_from on runtime criteria only", "code", True,
                           detail="no criteria_from in the plan")
    enumerated = _ENUMERATION.search(plan.request or "")
    if not enumerated:
        return CheckResult("L10", "criteria_from on runtime criteria only", "code", True,
                           detail="the request does not enumerate criteria")
    ids = ", ".join(q.id for q in templated)
    return CheckResult(
        "L10", "criteria_from on runtime criteria only", "code", False,
        detail=(f"the request already names the criteria ({enumerated.group(0).strip()!r}), "
                f"so {ids} must be one question per criterion, not a template"),
    )


# -- JEV checks ------------------------------------------------------------


@dataclass(frozen=True)
class _JevCheck:
    id: str
    name: str
    instructions: str
    state: Any
    defect_if_yes: bool = False
    target: str | None = None


def jev_checks(plan: Plan, jev: JevClient) -> list[CheckResult]:
    specs = _build_jev_checks(plan)
    results: list[CheckResult] = []
    for state, group in _group_by_state(specs):
        # The id names the check, so a failure is readable in a raw JEV log.
        questions = [
            Question(id=f"{spec.id}#{i}", type=QuestionType.NOUL,
                     instructions=spec.instructions)
            for i, spec in enumerate(group)
        ]
        answers = jev.ask(state, questions)
        for i, spec in enumerate(group):
            answer = answers.get(f"{spec.id}#{i}")
            value = float(answer.probability or 0.0) if answer else 0.0
            passed = value < LINT_THRESHOLD if spec.defect_if_yes else value >= LINT_THRESHOLD
            results.append(
                CheckResult(spec.id, spec.name, "jev", passed,
                            detail=spec.instructions, score=value, target=spec.target)
            )
    return results


def _build_jev_checks(plan: Plan) -> list[_JevCheck]:
    plan_json = plan.to_json()
    specs: list[_JevCheck] = []

    # L5 — only for the asks code could not settle.
    if not plan.jev_role.is_core and not _same_text(plan.residual_prompt, plan.request):
        state = {"request": plan.request, "residual_prompt": plan.residual_prompt}
        for i, ask in enumerate(plan.asks):
            specs.append(_JevCheck(
                "L5", "ask transmitted",
                f"`residual_prompt` must still ask for this: \"{ask}\". "
                "Does it, either directly or through a placeholder standing for a "
                "decision already taken?",
                state, target=f"asks[{i}]",
            ))

    # L6 — asks verified, post roles only.
    if plan.jev_role.has_post:
        state = {"asks": list(plan.asks),
                 "post_checks": [c.to_dict() for c in plan.post_checks]}
        for i, ask in enumerate(plan.asks):
            specs.append(_JevCheck(
                "L6", "ask verified",
                f"Is this requirement directly verified by at least one of the post checks: "
                f"\"{ask}\"?",
                state, target=f"asks[{i}]",
            ))

    # L7 — atomic questions.
    if plan.questions:
        specs.append(_JevCheck(
            "L7", "questions are atomic",
            "Can every question in `plan` be answered on its own, without knowing the "
            "outcome of any other question in the same round?",
            {"plan": plan_json},
        ))

    # L8 — no additions in the residual prompt.
    if plan.residual_prompt:
        specs.append(_JevCheck(
            "L8", "residual prompt adds nothing",
            "Does `residual_prompt` ask only for what `request` asks for, with no added "
            "requirement of tone, length, structure, content or quality? Placeholders "
            "carrying context that was already decided do not count as additions.",
            {"request": plan.request, "residual_prompt": plan.residual_prompt},
        ))

    # L9 — non-exclusive choice options.
    for q in plan.questions:
        if q.type is QuestionType.CHOICE:
            specs.append(_JevCheck(
                "L9", "choice options are exclusive",
                "Can two or more of these options be true at the same time for the same item?",
                {"question": q.to_dict()}, defect_if_yes=True, target=q.id,
            ))

    # L11 — a holistic judgment where criteria_from was needed.
    if plan.questions and not any(q.criteria_from for q in plan.questions):
        specs.append(_JevCheck(
            "L11", "no holistic judgment over runtime criteria",
            "Does `plan` judge with a single question a set of criteria that exist only "
            "in the runtime input, instead of one question per criterion?",
            {"request": plan.request, "plan": plan_json}, defect_if_yes=True,
        ))

    # L12 — a decision rule the user never stated. Escalation is excluded: it is
    # mandatory, and counting it made the v1 wording unusable (every plan 0.60-0.86).
    if plan.policy or plan.residual_prompt:
        specs.append(_JevCheck(
            "L12", "no invented decision rule",
            "Ignoring any escalation rule, does `policy` or `residual_prompt` apply a "
            "decision rule, restriction or criterion that `request` does not state?",
            {"request": plan.request,
             "policy": [r.to_dict() for r in plan.policy],
             "residual_prompt": plan.residual_prompt},
            defect_if_yes=True,
        ))

    # L13 — answerable from the state alone.
    for q in plan.questions:
        state = plan.state_of(q.id)
        specs.append(_JevCheck(
            "L13", "answerable from the state",
            "Can this question be answered reliably using only the content of the state, "
            "without external facts?",
            {"question": q.to_dict(), "state": state.to_dict() if state else None},
            target=q.id,
        ))

    return specs


def _group_by_state(specs: Sequence[_JevCheck]) -> list[tuple[Any, list[_JevCheck]]]:
    """One ``jev_ask`` per distinct state: many questions, one state, as intended."""
    groups: list[tuple[Any, list[_JevCheck]]] = []
    for spec in specs:
        key = json.dumps(spec.state, sort_keys=True, default=str)
        for existing_key, group in groups:
            if existing_key == key:
                group.append(spec)
                break
        else:
            groups.append((key, [spec]))
    return [(json.loads(key), group) for key, group in groups]


def _same_text(a: str | None, b: str | None) -> bool:
    return _normalize(a) == _normalize(b) and _normalize(a) != ""


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded).strip()
