# bugs.md 核查结论

本文档用于核对 [bugs.md](/Users/jianjian/code/python-study/python-cursor2api/bugs.md) 中列出的问题是否在当前代码中真实存在。

## 结论汇总

| # | 原条目 | 结论 | 说明 |
|---|---|---|---|
| 1 | `extract_cc_text` 忽略 `tool_use` | 真实存在 | 会丢失 `tool_use` block |
| 2 | 流式响应中 `error` 事件未正确处理 | 真实存在 | `error` 被当普通文本流出 |
| 3 | 流式响应收到 `error` 后不会终止 | 部分成立 | 不会立刻 `break`，但 worker 很快会放入 `None` 结束 |
| 4 | `thread.join` 在生成器中可能不被执行 | 真实存在 | 客户端断开时存在后台线程继续运行风险 |
| 5 | `build_agent_messages` 扁平化 assistant 内容 | 真实存在 | 会丢失 `tool_use` 结构 |
| 6 | `run_agent_loop` 混用两种工具协议 | 不足以认定为 bug | 更像兼容旧协议的设计，只有潜在误判风险 |
| 7 | `_max_steps` 是全局可变变量 | 不足以认定为 bug | 属于架构隐患，不是当前已证实错误 |
| 8 | `/v1/messages` 缺少请求体校验和错误处理 | 真实存在 | 非法输入会触发 500 |
| 9 | `sse_event` 对 `ping` 发送空 JSON | 真实存在 | 当前实现与预期 SSE 结构不一致 |

## 逐项说明

### 1. `extract_cc_text` 忽略 `tool_use`

- 位置：[server.py:38](/Users/jianjian/code/python-study/python-cursor2api/server.py#L38)
- 判定：真实存在
- 依据：
  - 代码只处理 `text` 和 `tool_result`。
  - `tool_use` block 没有任何分支处理。
  - 最小复现中，含 `tool_use` 的 content 被提取后，这部分内容直接消失。

### 2. 流式响应中 `error` 事件未正确处理

- 位置：[server.py:130](/Users/jianjian/code/python-study/python-cursor2api/server.py#L130)、[server.py:176](/Users/jianjian/code/python-study/python-cursor2api/server.py#L176)
- 判定：真实存在
- 依据：
  - worker 捕获异常后会 `q.put(("error", str(e)))`。
  - 生成器没有单独处理 `error`。
  - `event_type != "final"` 时统一走 `data + "\n"`，再包装成 `content_block_delta`。
  - 最小复现中，`("error", "boom")` 会变成普通文本增量 `"boom\n"` 发给客户端。

### 3. 流式响应收到 `error` 后不会终止

- 位置：[server.py:173](/Users/jianjian/code/python-study/python-cursor2api/server.py#L173)
- 判定：部分成立
- 依据：
  - 收到 `("error", ...)` 后确实不会立即 `break`。
  - 但 worker 的 `finally` 一定会执行 `q.put(None)`，所以通常下一次取队列就会结束。
  - 因此“错误后继续无限等待”这个说法不准确。
  - 真正成立的问题是：错误没有作为独立错误事件发出，而是混入正常文本流，这一点已由第 2 条覆盖。

### 4. `thread.join` 在生成器中可能不被执行

- 位置：[server.py:199](/Users/jianjian/code/python-study/python-cursor2api/server.py#L199)
- 判定：真实存在
- 依据：
  - `thread.join(timeout=10)` 放在生成器尾部，而不是 `try/finally`。
  - 如果客户端中途中断 SSE 消费，生成器可能不会执行到末尾。
  - worker 线程为 `daemon=True`，不会卡住进程退出，但任务继续运行、继续消耗资源的风险真实存在。

### 5. `build_agent_messages` 扁平化 assistant 消息内容

- 位置：[server.py:57](/Users/jianjian/code/python-study/python-cursor2api/server.py#L57)
- 判定：真实存在
- 依据：
  - assistant 消息先经过 `extract_cc_text` 变成纯文本。
  - 然后重新包装成单一 `{"type": "text", "text": ...}`。
  - 这样会丢失原有 `tool_use` 结构。
  - 最小复现可见，原始 assistant content 中的 `tool_use` 不会保留下来。

### 6. `run_agent_loop` 混用两种工具调用协议

- 位置：[main.py:364](/Users/jianjian/code/python-study/python-cursor2api/main.py#L364)
- 判定：不足以认定为 bug
- 依据：
  - 当前逻辑确实同时支持：
    - 原生 `tool_use` block
    - 文本中的 JSON 协议
  - 这会带来维护复杂度，也存在边界情况：
    - 如果模型返回的纯文本刚好是 JSON 对象，可能被误当作旧协议处理。
  - 但从当前代码无法证明这会在正常路径中稳定触发错误。
  - 更准确的定性应是“兼容旧协议的设计存在潜在误判风险”，而不是已证实 bug。

### 7. `_max_steps` 是 module 级全局可变变量

- 位置：[server.py:26](/Users/jianjian/code/python-study/python-cursor2api/server.py#L26)
- 判定：不足以认定为 bug
- 依据：
  - `_max_steps` 只在启动时于 [server.py:273](/Users/jianjian/code/python-study/python-cursor2api/server.py#L273) 附近赋值。
  - 请求处理中是只读使用，没有看到运行期并发修改。
  - 这是架构可改进点，但不是当前已经观察到的功能错误。

### 8. `/v1/messages` 缺少请求体校验和错误处理

- 位置：[server.py:103](/Users/jianjian/code/python-study/python-cursor2api/server.py#L103)
- 判定：真实存在
- 依据：
  - 端点直接 `await request.json()`，没有显式异常处理。
  - `messages` 也没有做类型校验。
  - 实测：
    - 非法 JSON 请求返回 `500`
    - `{"messages": ["x"]}` 也返回 `500`
  - `bugs.md` 中“FastAPI 会抛出 422”这一句不准确，因为这里没有使用请求模型校验。

### 9. `ping` 事件发送空 JSON 对象

- 位置：[server.py:90](/Users/jianjian/code/python-study/python-cursor2api/server.py#L90)、[server.py:170](/Users/jianjian/code/python-study/python-cursor2api/server.py#L170)
- 判定：真实存在
- 依据：
  - 当前代码在超时时发送 `yield sse_event("ping", {})`。
  - 实际生成结果是：
    - `event: ping`
    - `data: {}`
  - 从当前服务其他事件的格式风格看，这个 `ping` 结构不完整，至少存在协议一致性问题。

## 最终判断

可以确认真实存在的问题：

- Bug 1
- Bug 2
- Bug 4
- Bug 5
- Bug 8
- Bug 9

部分成立，建议并入其他问题表述：

- Bug 3

更适合归类为设计/架构风险，而非当前已证实 bug：

- Bug 6
- Bug 7
