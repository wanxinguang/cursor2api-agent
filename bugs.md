# server.py Bug 分析报告

## Bug 1: `extract_cc_text` 忽略 `tool_use` 类型的 block

**位置:** `server.py:38-54`

**问题描述:**
`extract_cc_text` 函数在遍历 content list 时，只处理了 `type == "text"` 和 `type == "tool_result"` 的 block，对 `tool_use` block（模型调用工具时返回的结构）完全忽略，导致工具调用内容在提取文本时丢失。

**问题代码:**
```python
if block.get("type") == "text":
    texts.append(block.get("text", ""))
elif block.get("type") == "tool_result":
    ...
# tool_use block 没有被处理
```

**影响:** 多轮对话中，assistant 消息含有 tool_use block 时，这部分内容会被静默丢弃，造成上下文信息不完整。

---

## Bug 2: 流式响应中 `error` 事件未正确处理

**位置:** `server.py:130-137` (worker) 和 `server.py:166-184` (event_generator)

**问题描述:**
worker 线程在捕获异常时会向队列推送 `("error", str(e))`，但 `event_generator` 中没有对 `error` 事件类型做特殊处理。当前逻辑：

```python
event_type, data = item
text = data if event_type == "final" else data + "\n"
```

`error` 事件会走 `else` 分支，被当成普通文本内容（还会额外加一个换行符）发送给客户端，客户端无法区分这是错误信息还是正常输出。

**影响:** 错误信息被悄悄混入正常输出流，客户端无法感知发生了错误，也无法提前终止流。

---

## Bug 3: 流式响应收到 `error` 事件后不会终止

**位置:** `server.py:173-184`

**问题描述:**
收到 `("error", ...)` 后，循环不会 break，会继续等待下一个队列项直到收到 `None`。这意味着：
1. 错误发生后，客户端还要等 worker 线程的 `finally: q.put(None)` 才能结束流。
2. 错误内容被混入正常的 `content_block_delta` SSE 事件中，格式不符合 Anthropic API 规范（应该用 `error` 类型事件）。

**修复建议:** 收到 `error` 事件时，发送符合规范的错误响应并 break。

---

## Bug 4: `thread.join` 在生成器中可能不被执行

**位置:** `server.py:199`

**问题描述:**
`thread.join(timeout=10)` 写在 `event_generator` 生成器的末尾。如果客户端中途断开连接，FastAPI/uvicorn 会停止迭代生成器，导致 `thread.join` 永远不会执行。虽然 thread 设置了 `daemon=True` 不会阻塞进程退出，但 worker 线程中运行的 `run_agent_loop` 不会被通知停止，会继续消耗资源直到完成或进程退出。

**影响:** 客户端断开后，后端 agent 任务仍在运行，浪费计算资源和 API 调用次数。

---

## Bug 5: `build_agent_messages` 将 assistant 消息内容扁平化为纯文本

**位置:** `server.py:57-72`

**问题描述:**
对 assistant 消息，函数先调用 `extract_cc_text` 将 content 提取为纯文本字符串，再包装成 `[{"type": "text", "text": ...}]`。

```python
text = extract_cc_text(msg.get("content", ""))
agent_messages.append({
    "role": "assistant",
    "content": [{"type": "text", "text": text}],
})
```

这会丢失原始 content 中的 `tool_use` block 结构。Anthropic API 要求：如果 assistant 消息含有 `tool_use` block，后续 user 消息必须包含对应的 `tool_result` block，否则 API 会返回错误。

**影响:** 多轮对话中，如果历史消息包含工具调用，转换后的消息格式不合法，会导致 API 请求失败。

---

## Bug 6: `run_agent_loop` 中两种工具调用协议混用

**位置:** `main.py:384-430`

**问题描述:**
`run_agent_loop` 中同时存在两套工具调用处理逻辑：
- **第 384-406 行：** 处理原生 Claude API 的 `tool_use` block 格式（标准格式）
- **第 408-430 行：** 当没有 `tool_use` block 时，尝试从文本中用 `parse_model_json` 解析 JSON 格式的工具调用（旧式/自定义格式）

这两种协议在同一个 agent loop 中混用，逻辑混乱。如果模型返回了纯文本回答（非工具调用），第 408 行的 `parse_model_json` 解析会失败并 `yield ("final", text_output)` 返回，这是正确的；但如果文本中恰好含有看似 JSON 的内容，可能被误识别为工具调用。

**影响:** 逻辑分支复杂，难以维护，且在某些边界情况下行为不可预测。

---

## Bug 7: `_max_steps` 是 module 级别的全局可变变量

**位置:** `server.py:26`

**问题描述:**
```python
_max_steps: int = 10
```

`_max_steps` 是 module 全局变量，在 `main()` 启动时被设置一次。虽然运行期间不会被并发修改（仅在 `create_message` 中只读使用），但这种模式是隐患——全局可变状态在多线程/异步环境中容易出错，且不利于测试和扩展（例如未来支持每请求自定义 max_steps）。

**影响:** 当前影响有限，但架构上存在风险。

---

## Bug 8: `/v1/messages` 端点缺少请求体校验和错误处理

**位置:** `server.py:103-119`

**问题描述:**
`create_message` 端点直接调用 `request.json()`，如果请求体不是合法 JSON，FastAPI 会抛出 422 错误，这是合理的。但对 `messages` 字段为空、`content` 字段格式异常等情况没有任何校验，会直接传入后续函数，导致难以定位的内部错误。

```python
cc_messages = body.get("messages", [])  # 可能是非列表类型
agent_messages = build_agent_messages(cc_messages)  # 直接使用，无校验
```

**影响:** 非法请求会触发 500 内部错误，而非返回规范的 400 错误响应。

---

## Bug 9: `sse_event` 函数对 `ping` 事件发送空 JSON 对象

**位置:** `server.py:90-92` 和 `server.py:170`

**问题描述:**
队列超时时发送 `ping` 事件：
```python
yield sse_event("ping", {})
```

Anthopic API 的 SSE 规范中，`ping` 事件的 data 应为 `{"type": "ping"}`，而这里发送的是 `{}`，不符合规范。

**影响:** 客户端若严格校验 ping 事件格式，可能会报错或忽略该事件，导致连接保活机制失效。

---

## 汇总

| # | 位置 | 严重程度 | 类型 |
|---|------|----------|------|
| 1 | `server.py:38-54` | 中 | 逻辑缺失 |
| 2 | `server.py:130-137, 166-184` | 高 | 错误处理缺失 |
| 3 | `server.py:173-184` | 高 | 控制流错误 |
| 4 | `server.py:199` | 低 | 资源泄漏风险 |
| 5 | `server.py:57-72` | 高 | 数据格式错误 |
| 6 | `main.py:384-430` | 中 | 逻辑混乱 |
| 7 | `server.py:26` | 低 | 架构风险 |
| 8 | `server.py:103-119` | 中 | 缺少输入校验 |
| 9 | `server.py:90-92, 170` | 低 | 规范不符 |
