"""``jev-drive``: pilot a run from outside, one model call at a time.

The loop is always the same four steps:

    jev-drive init  --state run.json --request "..." [--inputs inputs.json]
    jev-drive next  --state run.json          # -> the call to make, as JSON
    jev-drive submit --state run.json --result -   # <- its answer, on stdin
    ...repeat until 'next' prints the result instead of a call...
    jev-drive result --state run.json         # the answer for the user

Everything lives in the transcript file, so the loop survives between turns,
processes and machines.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .driver import Driver, DriverError, Transcript
from .pipeline import PipelineResult


def _load(path: Path) -> Driver:
    if not path.exists():
        raise SystemExit(f"no transcript at {path}; run 'jev-drive init' first")
    return Driver(Transcript.from_json(path.read_text(encoding="utf-8")))


def _save(path: Path, driver: Driver) -> None:
    path.write_text(driver.transcript.to_json() + "\n", encoding="utf-8")


def _read_payload(source: str, kind: str) -> Any:
    """An LLM completion is text, even when that text happens to be JSON.

    A compiled plan is a JSON object and would parse — but the pipeline wants
    the model's completion verbatim, so only JEV answers are decoded.
    """
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    if kind == "llm":
        return text.strip()
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise DriverError(f"a jev answer must be JSON: {exc}") from exc


def _emit(step: Call_or_Result, verbose: bool) -> int:
    if isinstance(step, PipelineResult):
        print(json.dumps({"status": "done", **_summary(step)}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({"status": "pending", "call": step.to_dict()},
                     ensure_ascii=False, indent=2))
    return 0


Call_or_Result = Any


def _summary(result: PipelineResult) -> dict[str, Any]:
    return {
        "task_shape": result.gate.task_shape.value,
        "jev_role": result.gate.jev_role.value,
        "blocked": result.blocked,
        "escalated": result.escalated,
        "lint": None if result.lint is None else {
            "checks": len(result.lint.checks),
            "failed": [c.as_feedback() for c in result.lint.failures],
            "blocked": result.lint.blocked,
        },
        "notes": result.notes,
        "text": result.text,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-drive", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def with_state(p):
        p.add_argument("--state", required=True, type=Path, help="the transcript file")
        return p

    init = with_state(sub.add_parser("init", help="start a run"))
    init.add_argument("--request", required=True, help="the user's request, verbatim")
    init.add_argument("--inputs", type=Path, help="JSON object binding the plan's placeholders")
    init.add_argument("--source", type=Path,
                      help="the source text, for the length checks of summaries")
    init.add_argument("--gate", choices=["v2", "v3"], default="v2")
    init.add_argument("--compiler-version", default="v0.4")
    init.add_argument("--lint", choices=["full", "code"], default="full",
                      help="'code' skips the semantic lint checks and the JEV calls they "
                           "cost; the blocking checks in code always run")
    init.add_argument("--force", action="store_true", help="overwrite an existing transcript")

    with_state(sub.add_parser("next", help="show the pending call, or the result"))

    submit = with_state(sub.add_parser("submit", help="record an answer"))
    submit.add_argument("--result", required=True,
                        help="file holding the answer, or '-' for stdin")
    submit.add_argument("--fingerprint", help="the call this answers, as a safety check")

    rewind = with_state(sub.add_parser("rewind", help="drop the last answers"))
    rewind.add_argument("--count", type=int, default=1)

    with_state(sub.add_parser("result", help="print the final answer for the user"))
    with_state(sub.add_parser("status", help="print a short human summary"))

    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except DriverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        if args.state.exists() and not args.force:
            raise SystemExit(f"{args.state} already exists; pass --force to overwrite")
        transcript = Transcript(
            request=args.request,
            inputs=json.loads(args.inputs.read_text(encoding="utf-8")) if args.inputs else {},
            source_text=args.source.read_text(encoding="utf-8") if args.source else None,
            gate=args.gate,
            compiler_version=args.compiler_version,
            lint_with_jev=args.lint == "full",
        )
        driver = Driver(transcript)
        _save(args.state, driver)
        return _emit(driver.step(), verbose=False)

    driver = _load(args.state)

    if args.command == "next":
        return _emit(driver.step(), verbose=False)

    if args.command == "submit":
        pending = driver.step()
        if isinstance(pending, PipelineResult):
            raise DriverError("the run is already finished; nothing is pending")
        step = driver.submit(_read_payload(args.result, pending.kind), args.fingerprint)
        _save(args.state, driver)
        return _emit(step, verbose=False)

    if args.command == "rewind":
        driver.rewind(args.count)
        _save(args.state, driver)
        return _emit(driver.step(), verbose=False)

    step = driver.step()
    if not isinstance(step, PipelineResult):
        print(f"still pending: {step.kind} call #{step.index} ({step.purpose})", file=sys.stderr)
        return 1

    if args.command == "result":
        print(step.text)
        return 0

    summary = _summary(step)
    print(f"{summary['task_shape']} / {summary['jev_role']}")
    if summary["lint"]:
        print(f"lint: {summary['lint']['checks']} checks, "
              f"{len(summary['lint']['failed'])} failed")
    for note in summary["notes"]:
        print(f"note: {note}")
    if summary["escalated"]:
        print("ESCALATED")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
