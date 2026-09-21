"""The synthesis stage: what the user actually reads.

The A/B comparison in docs/spec.md found the failure this stage exists to fix:
with a residual prompt the model writes the retention reply but never tells the
user that the message was read as a threat. So the answer carries three things
the generated text alone does not: the decisions taken in pre, any rule the
policy applied that the user never stated, and every disagreement between those
decisions and the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .checks import VerificationReport
from .client import JevClient
from .contract import Plan, PolicyRule
from .lint import LintReport
from .policy import PolicyOutcome
from .primitives import Answer, Question, QuestionType
from .runtime import RunResult, render_answer

#: A request that states its own condition ("se X, allora Y") is not inventing a
#: rule when the policy encodes it.
_CONDITIONAL = re.compile(
    r"\b(se|qualora|quando|altrimenti|if|when|unless|whenever|otherwise)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class Labels:
    decisions: str
    rule_applied: str
    confirm: str
    uncertain: str
    disagreement: str
    checks_failed: str
    escalated: str
    output: str


IT = Labels(
    decisions="Decisioni prese",
    rule_applied="Regola applicata",
    confirm="Non l'hai indicata tu: confermala.",
    uncertain="da verificare",
    disagreement="Il testo generato non concorda con la valutazione",
    checks_failed="Verifiche non superate",
    escalated="Escalation",
    output="Testo generato",
)

EN = Labels(
    decisions="Decisions taken",
    rule_applied="Rule applied",
    confirm="You did not state it: please confirm.",
    uncertain="to be checked",
    disagreement="The generated text disagrees with the assessment",
    checks_failed="Checks not passed",
    escalated="Escalation",
    output="Generated text",
)


@dataclass
class Synthesis:
    decisions: list[str] = field(default_factory=list)
    #: (rule, needs_confirmation). Every rule the policy applied is shown; only
    #: the ones the request never states ask the user to confirm them.
    applied_rules: list[tuple[str, bool]] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    failed_checks: list[str] = field(default_factory=list)
    escalated: bool = False
    output: str = ""
    labels: Labels = IT

    def render(self) -> str:
        lines: list[str] = []
        if self.decisions:
            lines.append(f"{self.labels.decisions}:")
            lines.extend(f"  - {d}" for d in self.decisions)
        if self.uncertain:
            lines.append(f"{self.labels.uncertain.capitalize()}:")
            lines.extend(f"  - {u}" for u in self.uncertain)
        for rule, needs_confirmation in self.applied_rules:
            suffix = f" {self.labels.confirm}" if needs_confirmation else ""
            lines.append(f"{self.labels.rule_applied}: {rule}{suffix}")
        for d in self.disagreements:
            lines.append(f"{self.labels.disagreement}: {d}")
        if self.failed_checks:
            lines.append(f"{self.labels.checks_failed}:")
            lines.extend(f"  - {c}" for c in self.failed_checks)
        if self.escalated:
            lines.append(f"{self.labels.escalated}: il risultato va rivisto da una persona."
                         if self.labels is IT else
                         f"{self.labels.escalated}: a person must review this result.")
        if self.output:
            lines.append("")
            lines.append(f"[{self.labels.output}]")
            lines.append(self.output)
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.render()


def applied_rules(
    plan: Plan, fired: Sequence[PolicyRule] | None = None, lint: LintReport | None = None
) -> list[tuple[str, bool]]:
    """Every decision rule the policy applied, and whether it needs confirming.

    Escalation is excluded: it is mandatory in every plan, and counting it is
    what made the JEV version of this check (L12) unusable. A rule needs
    confirming when the request states no condition of its own — or when L12
    reports that the plan invented one.
    """
    # Only what actually fired is "applied". Passing no list at all inspects
    # the plan itself, which is what the lint and the tests want.
    rules = list(plan.policy) if fired is None else list(fired)
    stated = bool(_CONDITIONAL.search(plan.request or ""))
    flagged_by_lint = bool(
        lint and any(c.id == "L12" and not c.passed for c in lint.checks)
    )
    needs_confirmation = flagged_by_lint or not stated
    return [(f"{r.when} -> {r.then}", needs_confirmation) for r in rules if not r.escalates]


def implicit_rules(
    plan: Plan, fired: Sequence[PolicyRule] | None = None, lint: LintReport | None = None
) -> list[str]:
    """Only the applied rules the user is asked to confirm."""
    return [rule for rule, needs in applied_rules(plan, fired, lint) if needs]


def describe_decisions(result: RunResult, plan: Plan) -> tuple[list[str], list[str]]:
    """Human-readable pre/core decisions, and the ones sitting in the uncertain band."""
    decisions: list[str] = []
    uncertain: list[str] = []
    for instance in result.instances:
        answer = result.answers.get(instance.question.id)
        if answer is None:
            continue
        question = plan.question(instance.base_id)
        prefix = f"{instance.item_key}: " if instance.item_key else ""
        text = (question.instructions if question else instance.base_id).rstrip("?")
        line = f"{prefix}{text} -> {_verdict(answer, question)}"
        decisions.append(line)
        if answer.is_uncertain:
            uncertain.append(line)
    return decisions, uncertain


def _verdict(answer: Answer, question: Question | None) -> str:
    if answer.type is QuestionType.NOUL:
        p = answer.probability or 0.0
        if answer.is_uncertain:
            return f"incerto ({p:.2f})"
        return f"sì ({p:.2f})" if p >= 0.5 else f"no ({p:.2f})"
    if answer.type is QuestionType.SCORE:
        levels = question.levels if question else ()
        level = answer.level
        label = levels[level].what if levels and level is not None and level < len(levels) else ""
        return f"{answer.score:.2f}{' (' + label + ')' if label else ''}"
    return f"{answer.option} ({answer.confidence or 0.0:.2f})"


def assessments(result: RunResult, plan: Plan) -> list[tuple[str, str]]:
    """Each pre/core decision as (question asked of the input, answer it got)."""
    pairs: list[tuple[str, str]] = []
    for instance in result.instances:
        answer = result.answers.get(instance.question.id)
        if answer is None:
            continue
        question = plan.question(instance.base_id)
        prefix = f"{instance.item_key}: " if instance.item_key else ""
        text = question.instructions if question else instance.base_id
        pairs.append((f"{prefix}{text}", render_answer(answer, question)))
    return pairs


def check_consistency(
    jev: JevClient,
    decisions: Sequence[str | tuple[str, str]],
    output: str,
    source: Any = None,
) -> list[str]:
    """Ask JEV whether the generated text agrees with the decisions injected into it.

    A decision is a question asked of the input, not of `output`. Handed over as
    a bare "question -> answer" line, JEV re-asks it of the output and answers
    "no" to a perfectly good reply (V26: 0.11). Passing the question and the
    answer apart, next to the input they were asked about, fixes that.
    """
    if not decisions or not output:
        return []
    about = "`input`" if source is not None else "the user's input"
    questions = [
        Question(
            id=f"d{i}",
            type=QuestionType.NOUL,
            instructions=(
                f"`assessments.d{i}` is a question that was asked about {about} and the "
                "answer it got. Does `output` take that answer into account when it "
                f"responds to {about}?"
            ),
        )
        for i in range(len(decisions))
    ]
    state: dict[str, Any] = {} if source is None else {"input": source}
    state["output"] = output
    state["assessments"] = {
        f"d{i}": {"question": d[0], "answer": d[1]} if isinstance(d, tuple) else d
        for i, d in enumerate(decisions)
    }
    answers = jev.ask(state, questions)
    out: list[str] = []
    for i, d in enumerate(decisions):
        a = answers.get(f"d{i}")
        value = float(a.probability or 0.0) if a else 0.0
        if value < 0.5:
            label = f"{d[0].rstrip('?')} -> {d[1]}" if isinstance(d, tuple) else d
            out.append(f"{label} ({value:.2f})")
    return out


def synthesize(
    plan: Plan,
    result: RunResult | None = None,
    policy: PolicyOutcome | None = None,
    verification: VerificationReport | None = None,
    output: str = "",
    disagreements: Sequence[str] = (),
    labels: Labels = IT,
    lint: LintReport | None = None,
) -> Synthesis:
    decisions, uncertain = describe_decisions(result, plan) if result else ([], [])
    fired = [t.rule for t in policy.triggered] if policy else []
    return Synthesis(
        decisions=decisions,
        applied_rules=applied_rules(plan, fired, lint),
        uncertain=uncertain,
        disagreements=list(disagreements),
        failed_checks=[f"{o.id}: {o.detail}" for o in (verification.failures if verification else [])],
        escalated=bool(policy and policy.escalated)
        or bool(verification and verification.escalate),
        output=output,
        labels=labels,
    )
