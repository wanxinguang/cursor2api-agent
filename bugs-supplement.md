# server.py & main.py Bug 补充分析报告

本报告是对 `bugs.md` 的进一步补充，重点关注协议一致性、资源生命周期管理以及多轮对话中的数据完整性。

---

## Bug 10: `user` 角色消息也被错误扁平化 (Bug 5 的延伸)

**位置:** `server.py:65-71`

**问题描述:**
不仅是 assistant 消息，`user` 角色的消息也被 `extract_cc_text` 强制转换成了纯文本字符串。

```python
if role == "user":
    if not agent_messages:
        text = f"Workspace root: {WORKSPACE_ROOT}\n\n{text}"
    agent_messages.append({"role": "user", "content": text})
```

**影响:** 
在 Anthropic API 规范中，如果 user 消息是对工具调用的响应，其 `content` 必须是一个包含 `type: "tool_result"` 的 **列表** 结构。强制转换为字符串会导致：
1. 模型无法识别之前的工具执行结果。
2. 破坏了“Assistant Tool Use -> User Tool Result”的严格序列要求，可能导致后续 API 调用直接报错。

---

## Bug 11: 同步响应模式下的错误状态码不正确

**位置:** `server.py:207-224` (`_sync_response`)

**问题描述:**
当 `run_agent_loop` 抛出异常（如 API 密钥错误、网络超时、步数超限）时，`_sync_response` 捕获了异常并将其包装在文本中返回：

```python
except Exception as e:
    final_text = f"Error: {e}"
```

**影响:** 
HTTP 响应状态码仍然是 `200 OK`。对于调用方而言，这是一种“伪成功”，自动化工具无法通过 HTTP 状态码识别出任务已失败，必须解析文本内容才能感知错误。

---

## Bug 12: 缺乏 `GeneratorExit` 捕获导致线程清理滞后 (Bug 4 的细化)

**位置:** `server.py:166-186` (`event_generator`)

**问题描述:**
SSE 生成器在客户端断开时会触发 `GeneratorExit` 异常。当前的 `event_generator` 逻辑没有显式捕获此异常并在 `finally` 块中确保线程回收。

```python
while True:
    try:
        item = q.get(timeout=30)
    # ... 缺少对 GeneratorExit 的处理
```

**影响:** 
虽然 `thread.join` 放在末尾，但由于生成器被中断，该行代码可能永远不会被执行。虽然 `daemon=True` 保证了进程退出时线程会销毁，但在长连接服务中，这会导致后台线程堆积，持续消耗 CPU 和内存。

---

## Bug 13: SSE 协议中的 Token 计数统计为虚假数据

**位置:** `server.py:146, 187`

**问题描述:**
在 `message_start` 和 `message_delta` 事件中，`input_tokens` 被硬编码为 `0`，而 `output_tokens` 使用了一个极其简陋的估算公式 `len(text) // 4`。

**影响:** 
下游应用或代理如果依赖这些字段进行计费、配额管理或性能监控，将获得完全错误的数据。这不符合“代理服务器（Proxy）”应尽可能透传上游真实指标的原则。

---

## Bug 14: `extract_cc_text` 对非文本 Block（如图片）的静默丢弃

**位置:** `server.py:43-52`

**问题描述:**
如果用户发送的消息中包含图片 block（`type: "image"`），`extract_cc_text` 会因为没有匹配的分支而直接忽略它。

**影响:** 
模型将完全丢失视觉上下文。虽然当前 agent 定位可能仅限文本/代码，但在处理来自 Claude Code 客户端的历史记录时，这种静默丢弃会导致模型理解出现断层（例如用户说“看这张截图里的报错”）。

---

## 修正建议汇总

1.  **重构消息转换**: 修改 `build_agent_messages`，使其保留 `tool_use` 和 `tool_result` 的原始结构，仅对纯文本消息进行 Workspace 路径增强。
2.  **增强生成器健壮性**: 在 `event_generator` 中添加 `try...finally` 块，确保无论客户端如何断开，都能触发 `thread.join()` 或设置停止信号。
3.  **标准化错误响应**: 针对同步请求，在捕获异常后应返回 `JSONResponse(status_code=500, ...)`。
4.  **改进 Token 统计**: 从 `client.call()` 的响应中提取真实的 `usage` 数据，并通过队列传递给生成器。
