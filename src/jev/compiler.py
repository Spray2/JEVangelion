"""The compiler stage: request + gate decision -> plan.

The compiler never answers the request and never sees its input data: states
hold placeholders, and the real input arrives at runtime. The system prompt is
v0.4, kept verbatim in ``prompts/compiler_v0_4.txt`` — the decision in
docs/spec.md is to run it on the default tier (8/8 correct plans there against
3-4/8 on the economy tier with the same prompt).
"""

from __future__ import annotations

from importlib import resources
from typing import Sequence

from .client import LLMClient
from .contract import JevRole, Plan, PlanError, passthrough_plan
from .gate import GateDecision

COMPILER_PROMPT_VERSION = "v0.4"


def compiler_system_prompt(version: str = COMPILER_PROMPT_VERSION) -> str:
    name = f"compiler_{version.replace('.', '_')}.txt"
    return resources.files(__package__).joinpath("prompts").joinpath(name).read_text(encoding="utf-8")


class CompilationError(RuntimeError):
    """The model returned something that is not a plan."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw


def build_user_message(request: str, gate: GateDecision, feedback: Sequence[str] = ()) -> str:
    """The turn given to the compiler: the request, the gate's verdict, retry feedback."""
    lines = [
        f'Request: "{request}"',
        f"Gate: {gate.task_shape.value}, {gate.jev_role.value}.",
    ]
    if feedback:
        lines.append("")
        lines.append(
            "Your previous plan failed these checks. Fix exactly these points and "
            "change nothing else:"
        )
        lines.extend(f"- {item}" for item in feedback)
    return "\n".join(lines)


def compile_plan(
    llm: LLMClient,
    request: str,
    gate: GateDecision,
    feedback: Sequence[str] = (),
    version: str = COMPILER_PROMPT_VERSION,
) -> Plan:
    """Compile one plan. ``jev_role = none`` short-circuits without an LLM call."""
    if gate.jev_role is JevRole.NONE:
        return passthrough_plan(request, gate.task_shape, gate.rationale)

    raw = llm.complete(compiler_system_prompt(version), build_user_message(request, gate, feedback))
    try:
        plan = Plan.parse(raw, request=request)
    except PlanError as exc:
        raise CompilationError(str(exc), raw=raw) from exc
    # Rule 1: the gate's verdict is kept. A compiler that downgrades because the
    # input data is missing has confused compilation with execution (V10 on the
    # economy tier), so the gate wins.
    if plan.task_shape is not gate.task_shape or plan.jev_role is not gate.jev_role:
        plan = _with_gate(plan, gate)
    return plan


def _with_gate(plan: Plan, gate: GateDecision) -> Plan:
    from dataclasses import replace

    return replace(plan, task_shape=gate.task_shape, jev_role=gate.jev_role)
