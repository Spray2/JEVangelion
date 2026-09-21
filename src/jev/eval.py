"""Bench runner: gate accuracy over a dataset, against any bound JEV client.

Without a client it replays the signals recorded in docs/spec.md, which measures
this code rather than the model — useful as a regression, useless as evidence
about JEV. Bind a real client with ``--client package.module:factory`` to
measure the model.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .client import JevClient, RecordedJevClient
from .gate import GATE_V2, GATE_V3, GateDecision, GateVariant, run_gate
from .benchmarks.cases import DATASETS, BenchCase

GATES: dict[str, GateVariant] = {"v2": GATE_V2, "v3": GATE_V3}


@dataclass(frozen=True)
class CaseResult:
    case: BenchCase
    decision: GateDecision

    @property
    def shape_ok(self) -> bool:
        return self.decision.task_shape is self.case.shape

    @property
    def role_ok(self) -> bool:
        return not self.decision.escalate and self.decision.jev_role is self.case.role

    @property
    def got(self) -> str:
        if self.decision.escalate:
            return f"escalation ({self.decision.task_shape.value})"
        return f"{self.decision.task_shape.value}/{self.decision.jev_role.value}"

    @property
    def expected(self) -> str:
        return f"{self.case.shape.value}/{self.case.role.value}"


@dataclass
class BenchReport:
    dataset: str
    gate: str
    results: list[CaseResult] = field(default_factory=list)

    @property
    def shape_score(self) -> tuple[int, int]:
        return sum(r.shape_ok for r in self.results), len(self.results)

    @property
    def role_score(self) -> tuple[int, int]:
        return sum(r.role_ok for r in self.results), len(self.results)

    @property
    def escalations(self) -> list[CaseResult]:
        return [r for r in self.results if r.decision.escalate]

    def render(self) -> str:
        lines = [f"dataset {self.dataset} · gate {self.gate}", ""]
        lines.append(f"{'id':<5} {'expected':<22} {'got':<22} shape role")
        for r in self.results:
            lines.append(
                f"{r.case.id:<5} {r.expected:<22} {r.got:<22} "
                f"{'ok ' if r.shape_ok else 'ERR':<5} {'ok' if r.role_ok else 'ERR'}"
            )
        s_ok, s_tot = self.shape_score
        r_ok, r_tot = self.role_score
        lines += ["", f"task_shape {s_ok}/{s_tot}", f"jev_role   {r_ok}/{r_tot}"]
        if self.escalations:
            lines.append("escalations: " + ", ".join(r.case.id for r in self.escalations))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "gate": self.gate,
            "task_shape": {"ok": self.shape_score[0], "total": self.shape_score[1]},
            "jev_role": {"ok": self.role_score[0], "total": self.role_score[1]},
            "cases": [
                {"id": r.case.id, "expected": r.expected, "got": r.got,
                 "shape_ok": r.shape_ok, "role_ok": r.role_ok,
                 "signals": r.decision.signals}
                for r in self.results
            ],
        }


def run_bench(
    cases: Sequence[BenchCase],
    variant: GateVariant = GATE_V2,
    client_for: Callable[[BenchCase], JevClient] | None = None,
    dataset: str = "custom",
) -> BenchReport:
    report = BenchReport(dataset=dataset, gate=variant.version)
    for case in cases:
        client = client_for(case) if client_for else _replay_client(case, variant.version)
        report.results.append(CaseResult(case, run_gate(client, case.request, variant)))
    return report


def _replay_client(case: BenchCase, variant: str) -> RecordedJevClient:
    return RecordedJevClient(case.recorded_answers(variant))


def _load_client(spec: str) -> Callable[[BenchCase], JevClient]:
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit("--client wants 'package.module:factory'")
    factory = getattr(importlib.import_module(module_name), attr)
    return lambda case: factory()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the JEV gate bench.")
    parser.add_argument("--dataset", choices=sorted(DATASETS), default="v2")
    parser.add_argument("--gate", choices=sorted(GATES), default="v2")
    parser.add_argument("--client", help="package.module:factory returning a JevClient")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = run_bench(
        DATASETS[args.dataset],
        GATES[args.gate],
        _load_client(args.client) if args.client else None,
        dataset=args.dataset,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.render())
        if not args.client:
            print("\n(replay of the signals recorded in docs/spec.md: this measures the "
                  "derivation code, not the model)")
    return 0 if report.role_score[0] == report.role_score[1] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
