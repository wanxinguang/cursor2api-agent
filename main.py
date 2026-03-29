from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List

import requests

from executor import ExecutorError, execute_tool_call, parse_model_json
from tools import (
    TOOLS,
    WORKSPACE_ROOT,
    ToolError,
    approval_root_for_path,
    ensure_workspace,
    is_within_workspace,
    resolve_user_path,
    shell_access_targets,
    SHELL_TIMEOUT,
)


SYSTEM_PROMPT = """You are a local coding agent with tool access.

Rules:
1. Prefer native tool use when you need to inspect files, write files, list directories, or run shell commands.
2. Available tools:
   - write_file(path, content)
   - read_file(path)
   - list_dir(path)
   - run_shell(command)
3. All file paths are relative to the workspace root.
4. Shell commands run inside the workspace root.
5. Allowed shell commands are limited to: ls, node, python.
6. If a tool result contains an error, adjust your plan and continue.
7. When the task is complete, reply with a normal final text answer.
"""

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def build_tool_spec() -> List[Dict[str, Any]]:
    return [
        {
            "name": "write_file",
            "description": "Write UTF-8 text into a file under the workspace.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "read_file",
            "description": "Read a UTF-8 text file under the workspace.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "list_dir",
            "description": "List files and directories under the workspace.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "run_shell",
            "description": "Run an allowed shell command inside the workspace.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    ]


class Cursor2APIClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
        timeout: int = 60,
        auth_mode: str = "x-api-key",
        max_retries: int = 2,
        retry_backoff_seconds: float = 3.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.auth_mode = auth_mode
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.session = requests.Session()
        if self._should_disable_env_proxy():
            self.session.trust_env = False

    def _should_disable_env_proxy(self) -> bool:
        host = (urlparse(self.base_url).hostname or "").lower()
        return host in {"localhost", "127.0.0.1", "::1"}

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.auth_mode == "x-api-key":
            headers["x-api-key"] = self.api_key
        elif self.auth_mode == "none":
            pass
        else:
            raise ExecutorError(f"unsupported auth_mode: {self.auth_mode}")
        return headers

    def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = self.retry_backoff_seconds * (2 ** (attempt - 1))
                print(f"[warn] request failed ({exc.__class__.__name__}), retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue

            if response.status_code not in {429, 500, 502, 503, 504}:
                return response

            if attempt >= attempts:
                return response

            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = float(retry_after)
            else:
                delay = self.retry_backoff_seconds * (2 ** (attempt - 1))
            print(f"[warn] server returned {response.status_code}, retrying in {delay:.1f}s...")
            time.sleep(delay)

        if last_error is not None:
            raise ExecutorError(f"request failed after retries when calling {url}: {last_error}") from last_error
        raise ExecutorError(f"request failed after retries when calling {url}")

    def call(self, messages: List[Dict[str, Any]], system: str | None = None) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/messages"
        headers = self._build_headers()
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system if system is not None else SYSTEM_PROMPT,
            "messages": messages,
            "tools": build_tool_spec(),
        }
        response = self._request_with_retry("POST", url, headers=headers, json=payload)
        if response.status_code == 401:
            raise ExecutorError(
                f"authentication failed with 401 at {url}. "
                f"Current auth_mode={self.auth_mode}. Check api_key/token and auth mode."
            )
        if response.status_code == 429:
            raise ExecutorError(
                f"rate limited with 429 at {url}. Wait and retry, reduce request frequency, "
                "or lower conversation size/max_steps."
            )
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            body = response.text[:500]
            raise ExecutorError(
                f"http error {response.status_code} at {url}. response body: {body}"
            ) from exc
        return response.json()

    def get_models(self) -> List[str]:
        url = f"{self.base_url}/v1/models"
        response = self._request_with_retry("GET", url, headers=self._build_headers())
        if response.status_code == 401:
            raise ExecutorError(
                f"preflight authentication failed with 401 at {url}. "
                f"Current auth_mode={self.auth_mode}. Check api_key/token and auth mode."
            )
        if response.status_code == 429:
            raise ExecutorError(f"preflight rate limited with 429 at {url}")
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            body = response.text[:500]
            raise ExecutorError(
                f"preflight http error {response.status_code} at {url}. response body: {body}"
            ) from exc
        data = response.json()
        items = data.get("data", [])
        models: List[str] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                models.append(item["id"])
        return models


def load_config(config_path: str) -> Dict[str, Any]:
    path = Path(config_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("config file must contain a JSON object")
    return data


class SessionApprovals:
    def __init__(self) -> None:
        self.allowed_roots: set[Path] = set()

    def reset(self) -> None:
        self.allowed_roots.clear()

    def is_allowed(self, path: Path) -> bool:
        target = path.resolve()
        if target == WORKSPACE_ROOT:
            return True
        if WORKSPACE_ROOT in target.parents:
            return True
        return any(root == target or root in target.parents for root in self.allowed_roots)

    def require(self, action: str, path: Path) -> None:
        target = path.resolve()
        if self.is_allowed(target):
            return

        approval_root = approval_root_for_path(target)
        print("\nConfirmation required:")
        print(f"- action: {action}")
        print(f"- target: {target}")
        print(f"- allow this directory for current session: {approval_root}")
        answer = input("Allow? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise ToolError(f"user denied access to {target}")
        self.allowed_roots.add(approval_root)


def run_tool_with_approvals(tool_name: str, arguments: Dict[str, Any], approvals: SessionApprovals) -> dict:
    if tool_name in {"read_file", "write_file", "list_dir"}:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            raise ExecutorError(f"{tool_name} requires a string path")
        target = resolve_user_path(raw_path)
        within = is_within_workspace(target)
        approvals.require(action=tool_name, path=target)
        tool_args = dict(arguments)
        if within and Path(raw_path).is_absolute():
            tool_args["path"] = str(target.relative_to(WORKSPACE_ROOT))
        return TOOLS[tool_name](**tool_args, allow_absolute=not within)
    elif tool_name == "run_shell":
        command = arguments.get("command")
        if not isinstance(command, str):
            raise ExecutorError("run_shell requires a string command")
        targets = shell_access_targets(command)
        for target in targets:
            approvals.require(action=f"{tool_name}: {command}", path=target)
        return TOOLS[tool_name](**arguments, allow_external_args=bool(targets))

    tool_fn = TOOLS[tool_name]
    return tool_fn(**arguments)


def run_preflight(client: Cursor2APIClient, model: str) -> None:
    print("Running preflight checks...")
    print(f"[ok] config loaded: base_url={client.base_url}, model={model}, auth_mode={client.auth_mode}")
    try:
        models = client.get_models()
    except ExecutorError as exc:
        print(f"[warn] model-list preflight skipped: {exc}")
        print("[ok] continue with /v1/messages directly")
        return

    print(f"[ok] /v1/models returned {len(models)} model(s)")
    if not models:
        print("[warn] /v1/models returned no models; skip model-name validation")
        return

    if model not in models:
        available = ", ".join(models[:10])
        print(f"[warn] configured model not found in /v1/models: {model}")
        print(f"[warn] available models: {available}")
        print("[ok] continue with /v1/messages directly")
        return

    print(f"[ok] model available: {model}")


def execute_anthropic_tool_block(block: Dict[str, Any], approvals: SessionApprovals) -> Dict[str, Any]:
    tool_name = block.get("name")
    arguments = block.get("input", {})
    if not isinstance(arguments, dict):
        raise ExecutorError("tool_use input must be a JSON object")
    payload = {
        "done": False,
        "tool": tool_name,
        "arguments": arguments,
    }
    return execute_tool_call(payload, tool_runner=lambda name, args: run_tool_with_approvals(name, args, approvals))


def extract_text_content(blocks: List[Dict[str, Any]]) -> str:
    texts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
    return "\n".join(part for part in texts if part).strip()


def run_tool_auto(tool_name: str, arguments: Dict[str, Any]) -> dict:
    """Auto-approve all operations, including paths outside workspace."""
    if tool_name in {"read_file", "write_file", "list_dir"}:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            raise ExecutorError(f"{tool_name} requires a string path")
        target = resolve_user_path(raw_path)
        within = is_within_workspace(target)
        tool_args = dict(arguments)
        if within and Path(raw_path).is_absolute():
            tool_args["path"] = str(target.relative_to(WORKSPACE_ROOT))
        return TOOLS[tool_name](**tool_args, allow_absolute=not within)
    elif tool_name == "run_shell":
        command = arguments.get("command")
        if not isinstance(command, str):
            raise ExecutorError("run_shell requires a string command")
        targets = shell_access_targets(command)
        return TOOLS[tool_name](**arguments, allow_external_args=bool(targets))
    tool_fn = TOOLS[tool_name]
    return tool_fn(**arguments)


def run_agent_loop(
    client: Cursor2APIClient,
    messages: List[Dict[str, Any]],
    max_steps: int,
    system: str | None = None,
):
    """Generator yielding (event_type, data) tuples during agent execution.

    event_type: "status" for intermediate updates, "final" for the answer.
    Auto-approves workspace operations; rejects outside-workspace access.
    """
    messages = list(messages)
    for step in range(1, max_steps + 1):
        response_data = client.call(messages, system=system)
        content = response_data.get("content", [])
        if not isinstance(content, list):
            raise ExecutorError(f"unexpected model response: {json.dumps(response_data, ensure_ascii=False)}")

        messages.append({"role": "assistant", "content": content})

        tool_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if tool_blocks:
            tool_results = []
            for block in tool_blocks:
                tool_name = block.get("name", "unknown")
                yield ("status", f"[Agent step {step}: executing {tool_name} ...]")
                try:
                    payload = {
                        "done": False,
                        "tool": block.get("name"),
                        "arguments": block.get("input", {}),
                    }
                    outcome = execute_tool_call(payload, tool_runner=run_tool_auto)
                    result = outcome.get("result", {})
                except ExecutorError as exc:
                    result = {"ok": False, "error": str(exc)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": json.dumps(result, ensure_ascii=False),
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        text_output = extract_text_content(content)
        if text_output:
            try:
                payload = parse_model_json(text_output)
            except ExecutorError:
                yield ("final", text_output)
                return

            outcome = execute_tool_call(payload, tool_runner=run_tool_auto)
            if outcome["done"]:
                yield ("final", str(outcome["final_answer"]))
                return

            yield ("status", f"[Agent step {step}: executing {outcome['tool']} ...]")
            messages.append({
                "role": "user",
                "content": (
                    "Tool execution result:\n"
                    f"{json.dumps(outcome, ensure_ascii=False, indent=2)}\n"
                    "Continue and return the next JSON object."
                ),
            })
            continue

        raise ExecutorError(f"unexpected model response: {json.dumps(response_data, ensure_ascii=False)}")

    raise ExecutorError(f"max steps exceeded: {max_steps}")


def run_agent(client: Cursor2APIClient, task: str, max_steps: int) -> None:
    ensure_workspace()
    approvals = SessionApprovals()
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Workspace root: {WORKSPACE_ROOT}\n"
                f"User task: {task}\n"
                "Decide the next action and return JSON."
            ),
        }
    ]
    run_agent_turn(client=client, messages=messages, max_steps=max_steps, approvals=approvals)


def run_agent_turn(
    client: Cursor2APIClient,
    messages: List[Dict[str, Any]],
    max_steps: int,
    approvals: SessionApprovals,
) -> str:
    for step in range(1, max_steps + 1):
        response_data = client.call(messages)
        content = response_data.get("content", [])
        if not isinstance(content, list):
            raise ExecutorError(f"unexpected model response: {json.dumps(response_data, ensure_ascii=False)}")

        print(f"\n=== step {step} ===")
        print(json.dumps(response_data, ensure_ascii=False, indent=2))

        messages.append({"role": "assistant", "content": content})

        tool_blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
        if tool_blocks:
            tool_results = []
            for block in tool_blocks:
                outcome = execute_anthropic_tool_block(block, approvals=approvals)
                print("\n=== tool_result ===")
                print(json.dumps(outcome["result"], ensure_ascii=False, indent=2))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("id"),
                        "content": json.dumps(outcome["result"], ensure_ascii=False),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue

        text_output = extract_text_content(content)
        if text_output:
            try:
                payload = parse_model_json(text_output)
            except ExecutorError:
                print("\n=== final ===")
                print(text_output)
                return text_output

            outcome = execute_tool_call(
                payload,
                tool_runner=lambda name, args: run_tool_with_approvals(name, args, approvals),
            )
            if outcome["done"]:
                print("\n=== final ===")
                print(outcome["final_answer"])
                return str(outcome["final_answer"])

            print("\n=== tool_result ===")
            print(json.dumps(outcome["result"], ensure_ascii=False, indent=2))
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Tool execution result:\n"
                        f"{json.dumps(outcome, ensure_ascii=False, indent=2)}\n"
                        "Continue and return the next JSON object."
                    ),
                }
            )
            continue

        raise ExecutorError(f"unexpected model response: {json.dumps(response_data, ensure_ascii=False)}")

    raise ExecutorError(f"max steps exceeded: {max_steps}")


def build_session_intro() -> str:
    return (
        f"Workspace root: {WORKSPACE_ROOT}\n"
        "You are in a persistent local agent session.\n"
        "Keep using prior conversation context unless the user changes direction.\n"
        "Use tools when needed, then respond normally when the task is complete."
    )


def read_user_input(prompt: str = "\n>>> ") -> str:
    try:
        return input(prompt).strip()
    except UnicodeDecodeError:
        print(prompt, end="", flush=True)
        line = sys.stdin.buffer.readline()
        if not line:
            return "exit"
        return line.decode("utf-8", errors="replace").strip()
    except EOFError:
        return "exit"


def run_interactive_session(client: Cursor2APIClient, opening_task: str | None, max_steps: int) -> None:
    ensure_workspace()
    approvals = SessionApprovals()
    intro = build_session_intro()
    if opening_task:
        first_content = f"{intro}\n\nUser task: {opening_task}"
    else:
        first_content = intro
    messages: List[Dict[str, Any]] = [{"role": "user", "content": first_content}]

    print("Interactive session started. Type 'exit' or 'quit' to stop. Type 'reset' or 'clear' to restart the conversation.")

    if opening_task:
        print(f"\n>>> {opening_task}")
        try:
            run_agent_turn(client=client, messages=messages, max_steps=max_steps, approvals=approvals)
        except ExecutorError as exc:
            print(f"\n[error] {exc}")

    while True:
        user_input = read_user_input()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Session closed.")
            return
        if user_input.lower() in {"reset", "clear"}:
            messages = [{"role": "user", "content": build_session_intro()}]
            approvals.reset()
            print("Conversation reset.")
            continue

        messages.append({"role": "user", "content": user_input})
        try:
            run_agent_turn(client=client, messages=messages, max_steps=max_steps, approvals=approvals)
        except ExecutorError as exc:
            print(f"\n[error] {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal local AI executor for cursor2api.")
    parser.add_argument("task", nargs="?", help="Task for the model to solve")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON file")
    parser.add_argument("--base-url", help="cursor2api base URL")
    parser.add_argument("--api-key", help="cursor2api API key")
    parser.add_argument("--model", help="Model name exposed by cursor2api")
    parser.add_argument("--max-steps", type=int, help="Maximum agent loop steps")
    parser.add_argument("--max-tokens", type=int, help="Maximum tokens per model response")
    parser.add_argument("--timeout-seconds", type=int, help="HTTP timeout for each model request")
    parser.add_argument("--auth-mode", help="Auth mode: bearer, x-api-key, or none")
    parser.add_argument("--max-retries", type=int, help="Automatic retry count for 429 and transient request failures")
    parser.add_argument("--retry-backoff-seconds", type=float, help="Base backoff seconds for automatic retries")
    parser.add_argument("--shell-timeout", type=int, help="Timeout in seconds for shell commands (default 30)")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip startup connectivity and model checks")
    parser.add_argument("--once", action="store_true", help="Run a single task instead of interactive session")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    base_url = args.base_url or config.get("base_url") or os.getenv("CURSOR2API_BASE_URL") or "http://localhost:3000"
    api_key = args.api_key or config.get("api_key") or os.getenv("CURSOR2API_API_KEY") or "test-key"
    model = args.model or config.get("model") or os.getenv("CURSOR2API_MODEL") or "claude-3-5-sonnet"
    max_steps = args.max_steps or int(config.get("max_steps", 10))
    max_tokens = args.max_tokens or int(config.get("max_tokens", 4096))
    timeout_seconds = args.timeout_seconds or int(config.get("timeout_seconds", 120))
    auth_mode = args.auth_mode or config.get("auth_mode") or "x-api-key"
    max_retries = args.max_retries if args.max_retries is not None else int(config.get("max_retries", 2))
    retry_backoff_seconds = (
        args.retry_backoff_seconds
        if args.retry_backoff_seconds is not None
        else float(config.get("retry_backoff_seconds", 3))
    )
    shell_timeout = args.shell_timeout if args.shell_timeout is not None else int(config.get("shell_timeout", 30))
    task = args.task or config.get("task")

    if args.once and not task:
        raise ValueError("task is required for --once mode. Set it in config.json or pass it on the command line.")

    import tools as _tools_mod
    _tools_mod.SHELL_TIMEOUT = shell_timeout

    client = Cursor2APIClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout_seconds,
        auth_mode=auth_mode,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    if not args.skip_preflight:
        run_preflight(client=client, model=model)
    if args.once:
        run_agent(client=client, task=task, max_steps=max_steps)
        return

    run_interactive_session(client=client, opening_task=task, max_steps=max_steps)


if __name__ == "__main__":
    main()
