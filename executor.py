from __future__ import annotations

import json
from typing import Any, Callable, Dict

from tools import TOOLS, ToolError


class ExecutorError(Exception):
    pass


def parse_model_json(text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExecutorError(f"model did not return valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ExecutorError("model response must be a JSON object")
    return payload


def execute_tool_call(
    payload: Dict[str, Any],
    tool_runner: Callable[[str, Dict[str, Any]], dict] | None = None,
) -> Dict[str, Any]:
    if payload.get("done") is True:
        return {
            "done": True,
            "final_answer": payload.get("final_answer", ""),
        }

    tool_name = payload.get("tool")
    arguments = payload.get("arguments", {})

    if tool_name not in TOOLS:
        raise ExecutorError(f"unknown tool: {tool_name}")
    if not isinstance(arguments, dict):
        raise ExecutorError("arguments must be a JSON object")

    try:
        if tool_runner is None:
            tool_fn = TOOLS[tool_name]
            result = tool_fn(**arguments)
        else:
            result = tool_runner(tool_name, arguments)
    except TypeError as exc:
        raise ExecutorError(f"invalid arguments for {tool_name}: {exc}") from exc
    except ToolError as exc:
        result = {"ok": False, "error": str(exc)}

    return {
        "done": False,
        "tool": tool_name,
        "arguments": arguments,
        "result": result,
    }
