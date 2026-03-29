from __future__ import annotations

import argparse
import json
import queue as queue_mod
import threading
import uuid
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from main import (
    Cursor2APIClient,
    SYSTEM_PROMPT,
    load_config,
    run_agent_loop,
    DEFAULT_CONFIG_PATH,
)
from tools import WORKSPACE_ROOT, ensure_workspace

app = FastAPI(title="cursor2api Agent Server")

_client: Cursor2APIClient | None = None
_max_steps: int = 10


def get_client() -> Cursor2APIClient:
    assert _client is not None, "client not initialized"
    return _client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_cc_text(content: Any) -> str:
    """Extract plain text from Claude Code message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    c = block.get("content", "")
                    texts.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
                elif block.get("type") == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    texts.append(f"[tool_use: {name}] {json.dumps(inp, ensure_ascii=False)}")
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(t for t in texts if t)
    return str(content)


def build_agent_messages(cc_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Claude Code conversation to agent messages with workspace context."""
    agent_messages: List[Dict[str, Any]] = []
    for msg in cc_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            # Preserve tool_result list structure; only augment plain text first message
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                agent_messages.append({"role": "user", "content": content})
            else:
                text = extract_cc_text(content)
                if not agent_messages:
                    text = f"Workspace root: {WORKSPACE_ROOT}\n\n{text}"
                agent_messages.append({"role": "user", "content": text})
        elif role == "assistant":
            # Preserve original content blocks (including tool_use) to satisfy API requirements
            if isinstance(content, list):
                agent_messages.append({"role": "assistant", "content": content})
            else:
                text = extract_cc_text(content)
                agent_messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                })
    return agent_messages


def build_system_prompt(cc_system: Any) -> str:
    """Merge Claude Code system prompt with our agent system prompt."""
    parts = [SYSTEM_PROMPT]
    if cc_system:
        if isinstance(cc_system, str):
            parts.append(cc_system)
        elif isinstance(cc_system, list):
            for block in cc_system:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
    return "\n\n".join(parts)


def sse_event(event_type: str, data: Any) -> str:
    data_str = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    return f"event: {event_type}\ndata: {data_str}\n\n"


def generate_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"type": "invalid_request_error", "message": "request body must be valid JSON"}}, status_code=400)

    cc_messages = body.get("messages", [])
    if not isinstance(cc_messages, list):
        return JSONResponse({"error": {"type": "invalid_request_error", "message": "messages must be a list"}}, status_code=400)
    if not cc_messages:
        return JSONResponse({"error": {"type": "invalid_request_error", "message": "messages must not be empty"}}, status_code=400)

    cc_system = body.get("system")
    cc_model = body.get("model", "")
    cc_stream = body.get("stream", False)

    client = get_client()
    agent_messages = build_agent_messages(cc_messages)
    system = build_system_prompt(cc_system)
    ensure_workspace()

    if cc_stream:
        return _stream_response(client, agent_messages, system, cc_model)
    return _sync_response(client, agent_messages, system, cc_model)


def _stream_response(
    client: Cursor2APIClient,
    messages: List[Dict[str, Any]],
    system: str,
    model: str,
) -> StreamingResponse:
    q: queue_mod.Queue[tuple[str, str] | None] = queue_mod.Queue()

    def worker() -> None:
        try:
            for event_type, data in run_agent_loop(client, messages, _max_steps, system=system):
                q.put((event_type, data))
        except Exception as e:
            q.put(("error", str(e)))
        finally:
            q.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def event_generator():
        msg_id = generate_msg_id()

        yield sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model or client.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

        yield sse_event("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })

        output_tokens = 0
        error_occurred = False
        try:
            while True:
                try:
                    item = q.get(timeout=30)
                except queue_mod.Empty:
                    yield sse_event("ping", {"type": "ping"})
                    continue

                if item is None:
                    break

                event_type, data = item
                if event_type == "error":
                    yield sse_event("error", {
                        "type": "error",
                        "error": {"type": "api_error", "message": data},
                    })
                    error_occurred = True
                    break

                text = data if event_type == "final" else data + "\n"
                output_tokens += max(len(text) // 4, 1)

                yield sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                })
        finally:
            thread.join(timeout=10)

        if not error_occurred:
            yield sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": 0,
            })

            yield sse_event("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": max(output_tokens, 1)},
            })

            yield sse_event("message_stop", {"type": "message_stop"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sync_response(
    client: Cursor2APIClient,
    messages: List[Dict[str, Any]],
    system: str,
    model: str,
) -> JSONResponse:
    final_text = ""
    status_parts: List[str] = []

    try:
        for event_type, data in run_agent_loop(client, messages, _max_steps, system=system):
            if event_type == "status":
                status_parts.append(data)
            elif event_type == "final":
                final_text = data
    except Exception as e:
        return JSONResponse(
            {"error": {"type": "api_error", "message": str(e)}},
            status_code=500,
        )

    full_text = ""
    if status_parts:
        full_text = "\n".join(status_parts) + "\n\n"
    full_text += final_text

    return JSONResponse({
        "id": generate_msg_id(),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": full_text}],
        "model": model or client.model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": max(len(full_text) // 4, 1)},
    })


@app.get("/v1/models")
async def list_models():
    client = get_client()
    try:
        models = client.get_models()
        return JSONResponse({
            "data": [{"id": m, "object": "model"} for m in models],
            "object": "list",
        })
    except Exception as e:
        return JSONResponse({"error": {"type": "api_error", "message": str(e)}}, status_code=502)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def parse_server_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Anthropic-compatible API server wrapping cursor2api agent")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON")
    parser.add_argument("--port", type=int, help="Server port (default: from config or 9090)")
    parser.add_argument("--host", help="Server host (default: from config or 127.0.0.1)")
    return parser.parse_args()


def main() -> None:
    global _client, _max_steps

    args = parse_server_args()
    config = load_config(args.config)

    base_url = config.get("base_url", "http://localhost:3000")
    api_key = config.get("api_key", "test-key")
    model = config.get("model", "claude-3-5-sonnet")
    _max_steps = int(config.get("max_steps", 10))
    max_tokens = int(config.get("max_tokens", 4096))
    timeout_seconds = int(config.get("timeout_seconds", 120))
    auth_mode = config.get("auth_mode", "x-api-key")
    max_retries = int(config.get("max_retries", 2))
    retry_backoff = float(config.get("retry_backoff_seconds", 3))
    shell_timeout = int(config.get("shell_timeout", 30))

    import tools as _tools_mod
    _tools_mod.SHELL_TIMEOUT = shell_timeout

    _client = Cursor2APIClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout_seconds,
        auth_mode=auth_mode,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff,
    )

    host = args.host or config.get("server_host", "127.0.0.1")
    port = args.port or int(config.get("server_port", 9090))

    print(f"Starting agent server on {host}:{port}")
    print(f"Backend: {base_url} (model: {model})")
    print(f"Workspace: {WORKSPACE_ROOT}")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
