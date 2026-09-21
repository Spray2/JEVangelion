"""JEVangelion — a reference implementation of the JEV Prompt Compiler.

The pipeline in docs/spec.md, stage by stage:

    request -> gate (JEV) -> compiler (LLM) -> lint -> rounds (JEV)
            -> policy (code) -> generation (LLM) -> post checks (JEV) -> synthesis

Each stage is an ordinary Python object; JEV and the main LLM are ports
(:mod:`jev.client`) a host binds to real models.
"""

from .contract import JevRole, Plan, PlanError, TaskShape
from .primitives import (
    JEV_MODEL_VERSION,
    Answer,
    ChoiceOption,
    Question,
    QuestionRole,
    QuestionType,
    ScoreLevel,
)

__all__ = [
    "JEV_MODEL_VERSION",
    "Answer",
    "ChoiceOption",
    "JevRole",
    "Plan",
    "PlanError",
    "Question",
    "QuestionRole",
    "QuestionType",
    "ScoreLevel",
    "TaskShape",
]
__version__ = "0.1.0"
