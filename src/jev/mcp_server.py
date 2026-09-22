"""``jev-mcp``: the driver as an MCP server, so a chat client can pilot a run.

``jev-drive`` needs a shell. A chat client (the Claude Desktop Chat tab) has
none, but it can call MCP tools: this server exposes the same loop — start,
submit, next, rewind, result — as tools, and keeps each run's transcript on
disk under ``JEV_RUNS_DIR`` (default ``~/.jev/runs``).

The chat model plays both external systems, exactly as in driver mode: it
forwards ``kind: "jev"`` calls to the typesafe-jev MCP server and answers
``kind: "llm"`` calls itself. To spare it the wire-format translation, every
pending JEV call carries ``jev_ask_params``, ready to pass to ``jev_ask``.

    python -m jev.mcp_server        # stdio transport, for claude_desktop_config.json
"""

from __future__ import annotations

import functools
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cli import _summary
from .driver import Driver, DriverError, Transcript
from .pipeline import PipelineResult

INSTRUCTIONS = """\
Pilots the JEV Prompt Compiler pipeline one model call at a time.

Loop: jev_start -> (answer the pending call, then jev_submit) until status is
"done" -> show the user the final `text`.

For each pending `call`:
- kind "jev": call the typesafe-jev tool `jev_ask` with `params` set to
  `call.jev_ask_params`, in ONE call, then pass its raw result to jev_submit as
  `answer`. Never invent JEV answers: the run exists to measure them.
- kind "llm", purpose "compile the plan": `call.system` is the compiler prompt
  and `call.user` the message; reply as that model with ONLY the JSON plan
  object (no prose, no code fences) and submit it as `answer`.
- kind "llm", purpose "generate the text": answer `call.user` yourself and
  submit the plain text.
Always pass the call's `fingerprint`. Pass the user's request verbatim to
jev_start. If an answer was wrong, jev_rewind and redo it. When the gate
decides role "none" or escalates, say so: it is a valid outcome, not a failure.
"""


def runs_dir() -> Path:
    root = Path(os.environ.get("JEV_RUNS_DIR") or Path.home() / ".jev" / "runs")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(run_id: str) -> Path:
    if not run_id or any(c in run_id for c in "/\\.:"):
        raise DriverError(f"invalid run id {run_id!r}")
    return runs_dir() / f"{run_id}.json"


def _load(run_id: str) -> Driver:
    path = _path(run_id)
    if not path.exists():
        raise DriverError(f"no run {run_id!r}; start one with jev_start")
    return Driver(Transcript.from_json(path.read_text(encoding="utf-8")))


def _save(run_id: str, driver: Driver) -> None:
    _path(run_id).write_text(driver.transcript.to_json() + "\n", encoding="utf-8")


def jev_ask_params(state: Any, questions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The driver's questions in the shape the typesafe-jev server accepts.

    The server wants questions keyed by id, and a Choice's criteria as a map
    option -> description; a Score keeps its ordered list of levels.
    """
    wire: dict[str, Any] = {}
    for q in questions:
        entry: dict[str, Any] = {"type": q["type"], "instructions": q["instructions"]}
        criteria = q.get("criteria")
        if q["type"] == "choice" and criteria:
            entry["criteria"] = {c["id"]: c.get("what") for c in criteria}
        elif q["type"] == "score" and criteria:
            entry["criteria"] = [
                {"what": c["what"], "examples": list(c.get("examples") or [])} for c in criteria
            ]
        wire[q["id"]] = entry
    return {"state": state, "questions": wire}


def _view(run_id: str, step: Any) -> dict[str, Any]:
    if isinstance(step, PipelineResult):
        return {"run_id": run_id, "status": "done", **_summary(step)}
    call = step.to_dict()
    if call["kind"] == "jev":
        call["jev_ask_params"] = jev_ask_params(call["state"], call["questions"])
    return {"run_id": run_id, "status": "pending", "call": call}


def _decode_jev(answer: Any) -> Any:
    """Accept the typesafe-jev result however the chat client hands it over."""
    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except json.JSONDecodeError as exc:
            raise DriverError(f"a jev answer must be JSON: {exc}") from exc
    # An MCP tool result often arrives wrapped as {"result": "<json text>"}.
    if isinstance(answer, Mapping) and set(answer) == {"result"} and isinstance(answer["result"], str):
        return _decode_jev(answer["result"])
    return answer


# -- tools -----------------------------------------------------------------


def jev_start(
    request: str,
    inputs: dict[str, Any] | None = None,
    gate: str = "v2",
    lint: str = "code",
) -> dict[str, Any]:
    """Start a pipeline run and return its first pending call.

    request: the user's request, verbatim.
    inputs: the runtime data the plan's placeholders bind to, e.g.
        {"customer_message": "..."} or {"articles": [...]}.
    gate: "v2" (default) or "v3".
    lint: "code" (default; 6 calls on a pre+post case) or "full" (14 calls).
    """
    if gate not in ("v2", "v3") or lint not in ("code", "full"):
        raise DriverError("gate must be 'v2' or 'v3', lint 'code' or 'full'")
    driver = Driver(Transcript(
        request=request,
        inputs=dict(inputs or {}),
        gate=gate,
        lint_with_jev=lint == "full",
    ))
    run_id = uuid.uuid4().hex[:12]
    _save(run_id, driver)
    return _view(run_id, driver.step())


def jev_submit(run_id: str, fingerprint: str, answer: Any) -> dict[str, Any]:
    """Record the answer to the pending call and return the next one (or the result).

    answer: for a "jev" call, the raw result of typesafe-jev jev_ask (object or
        JSON text); for an "llm" call, the completion text.
    """
    driver = _load(run_id)
    pending = driver.step()
    if isinstance(pending, PipelineResult):
        raise DriverError("the run is already finished; nothing is pending")
    if pending.kind == "llm":
        payload = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        payload = payload.strip()
    else:
        payload = _decode_jev(answer)
    step = driver.submit(payload, fingerprint)
    _save(run_id, driver)
    return _view(run_id, step)


def jev_next(run_id: str) -> dict[str, Any]:
    """Show the pending call of a run, or its result if it is finished."""
    return _view(run_id, _load(run_id).step())


def jev_rewind(run_id: str, count: int = 1) -> dict[str, Any]:
    """Drop the last `count` answers and return the call that is pending again."""
    driver = _load(run_id)
    driver.rewind(count)
    _save(run_id, driver)
    return _view(run_id, driver.step())


def jev_result(run_id: str) -> dict[str, Any]:
    """The finished run: the text for the user plus gate, lint and escalation."""
    step = _load(run_id).step()
    if not isinstance(step, PipelineResult):
        raise DriverError(f"still pending: {step.kind} call #{step.index} ({step.purpose})")
    return _view(run_id, step)


TOOLS = (jev_start, jev_submit, jev_next, jev_rewind, jev_result)


def build_server():
    # Optional dependency: pip install "jevangelion[mcp]".
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    def reported(tool):
        # The SDK hides the text of unexpected exceptions; a DriverError
        # (wrong fingerprint, unknown run) is the model's to read and fix.
        @functools.wraps(tool)
        def wrapper(*args, **kwargs):
            try:
                return tool(*args, **kwargs)
            except DriverError as exc:
                raise ToolError(str(exc)) from exc
        return wrapper

    server = MCPServer(name="jev-drive", instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.tool()(reported(tool))
    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
