"""End-to-end orchestration of the stages in docs/spec.md.

    request -> gate -> compiler -> lint -> rounds -> policy
            -> generation -> post checks -> synthesis

The retry budgets are the ones the spec fixes and never exceeds: one compiler
retry on a failed lint, one regeneration on a failed post check, then escalate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from .checks import MAX_REGENERATIONS, VerificationReport, verify
from .client import JevClient, LLMClient
from .compiler import COMPILER_PROMPT_VERSION, compile_plan
from .contract import JevRole, Plan
from .gate import GATE_V2, SHAPE_CONFIDENCE_FLOOR, GateDecision, GateVariant, run_gate
from .lint import LintReport, lint_plan
from .policy import PolicyOutcome, run_policy
from .runtime import RunResult, fill_residual_prompt, resolved_facts, run_rounds
from .synthesis import (
    IT,
    Labels,
    Synthesis,
    assessments,
    check_consistency,
    synthesize,
)
from .checks import guess_language


@dataclass
class PipelineResult:
    request: str
    gate: GateDecision
    plan: Plan | None = None
    lint: LintReport | None = None
    lint_retried: bool = False
    rounds: RunResult | None = None
    policy: PolicyOutcome | None = None
    generated: str = ""
    verification: VerificationReport | None = None
    synthesis: Synthesis | None = None
    blocked: bool = False
    escalated: bool = False
    notes: list[str] = field(default_factory=list)
    #: What the user reads when the run stops before any text exists, so a
    #: stop is never an empty answer.
    message: str = ""

    @property
    def text(self) -> str:
        if self.synthesis:
            return self.synthesis.render()
        return self.generated or self.message


def run(
    request: str,
    *,
    jev: JevClient,
    llm: LLMClient,
    inputs: Mapping[str, Any] | None = None,
    source_text: str | None = None,
    gate_variant: GateVariant = GATE_V2,
    compiler_version: str = COMPILER_PROMPT_VERSION,
    labels: Labels | None = None,
    lint_with_jev: bool = True,
) -> PipelineResult:
    inputs = dict(inputs or {})
    labels = labels or _labels_for(request)

    gate = run_gate(jev, request, gate_variant)
    result = PipelineResult(request=request, gate=gate)
    if gate.escalate:
        result.escalated = True
        result.notes.append(f"gate escalated: {gate.rationale}")
        result.message = labels.unclear_request.format(
            shape=gate.task_shape.value, confidence=gate.shape_confidence,
            floor=SHAPE_CONFIDENCE_FLOOR,
        )
        return result

    if gate.jev_role is JevRole.NONE:
        # The gate's whole point: most requests never reach the compiler. The
        # runtime inputs still go along: the main LLM answers the data, not
        # just the request.
        result.generated = llm.complete("", _with_inputs(request, inputs, labels))
        result.notes.append("no JEV role: the request went straight to the main LLM")
        return result

    plan, lint, retried = _compile_and_lint(
        llm, jev if lint_with_jev else None, request, gate, compiler_version
    )
    result.plan, result.lint, result.lint_retried = plan, lint, retried
    if lint.blocked:
        result.blocked = True
        result.notes.append("lint blocked the plan: " + "; ".join(lint.feedback()))
        result.message = labels.plan_blocked.format(reasons="; ".join(lint.feedback()))
        return result
    if lint.advisory_failures:
        result.notes.append(
            "lint warnings carried forward: " + "; ".join(c.as_feedback()
                                                          for c in lint.advisory_failures)
        )

    facts: dict[str, str] = {}
    if plan.rounds:
        result.rounds = run_rounds(jev, plan, inputs)
        result.policy = run_policy(plan.policy, result.rounds.scopes())
        result.escalated = result.policy.escalated
        facts = resolved_facts(result.rounds, plan)

    if plan.jev_role.is_core:
        result.synthesis = synthesize(
            plan, result.rounds, result.policy, labels=labels, lint=lint
        )
        return result

    # Runtime inputs fill any {{name}} left in the residual prompt, so the main
    # LLM can see the data it is answering; decisions take precedence.
    bound = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
             for k, v in inputs.items()}
    prompt = fill_residual_prompt(plan, {**bound, **facts})
    result.generated = llm.complete("", prompt)

    if plan.jev_role.has_post:
        result.verification = verify(jev, plan, result.generated, source_text, attempts=1)
        attempt = 1
        while not result.verification.ok and attempt <= MAX_REGENERATIONS:
            attempt += 1
            retry_prompt = _retry_prompt(prompt, result.verification)
            result.generated = llm.complete("", retry_prompt)
            result.verification = verify(jev, plan, result.generated, source_text, attempts=attempt)
        if not result.verification.ok:
            result.escalated = True
            result.notes.append("post checks still failing after one regeneration")

    disagreements: list[str] = []
    if result.rounds is not None and result.generated:
        disagreements = check_consistency(
            jev,
            assessments(result.rounds, plan, result.policy),
            result.generated,
            source=inputs or None,
        )

    result.synthesis = synthesize(
        plan, result.rounds, result.policy, result.verification,
        output=result.generated, disagreements=disagreements, labels=labels, lint=lint,
    )
    result.escalated = result.escalated or result.synthesis.escalated
    return result


def _compile_and_lint(
    llm: LLMClient, jev: JevClient | None, request: str, gate: GateDecision, version: str
) -> tuple[Plan, LintReport, bool]:
    """``jev=None`` runs only the deterministic checks.

    The semantic checks cost one JEV call per distinct state — eight on a
    pre+post plan — which is a lot in an interactive loop. They are consultivo
    by design; the code checks, which block, always run.
    """
    plan = compile_plan(llm, request, gate, version=version)
    report = lint_plan(plan, jev)
    if report.ok:
        return plan, report, False
    # One retry, with the failed checks as feedback. A second failure proceeds
    # with a warning, unless a code check blocks.
    retry = compile_plan(llm, request, gate, feedback=report.feedback(), version=version)
    retry_report = lint_plan(retry, jev)
    if len(retry_report.failures) <= len(report.failures):
        return retry, retry_report, True
    return plan, report, True


def _retry_prompt(prompt: str, verification: VerificationReport) -> str:
    failed = "\n".join(f"- {o.detail}" for o in verification.failures)
    return f"{prompt}\n\nLa versione precedente non ha superato queste verifiche:\n{failed}"


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)


def _with_inputs(request: str, inputs: Mapping[str, Any], labels: Labels) -> str:
    if not inputs:
        return request
    data = "\n\n".join(f"{name}:\n{_as_text(value)}" for name, value in inputs.items())
    return f"{request}\n\n{labels.provided_data}:\n\n{data}"


def _labels_for(request: str) -> Labels:
    from .synthesis import EN

    return EN if guess_language(request) == "en" else IT
