# cursor2api-agent

一个轻量级的本地 AI 执行器，用于配合 Claude API 转发服务实现本地工具调用。

## 功能特性

- 支持通过 Claude API 进行本地工具调用
- 提供文件读写、目录列表、Shell 命令执行等工具
- 安全的沙箱机制，限制操作范围在 `workspace` 目录
- 支持交互式会话模式
- 支持单次任务执行模式

## 项目结构

```
├── main.py        # 主入口和 agent 循环
├── executor.py    # 工具调用执行器
├── tools.py       # 本地工具实现
├── server.py      # HTTP 服务器模式
├── config.json    # 配置文件
├── DESIGN.md      # 设计文档
└── workspace/     # 工作目录（所有文件操作限制在此目录）
```

## 安装

```bash
# 克隆仓库
git clone https://github.com/wanxinguang/cursor2api-agent.git
cd cursor2api-agent

# 安装依赖
pip install requests
```

## 配置

复制并编辑 `config.json`：

```json
{
  "base_url": "https://your-api-endpoint.com",
  "api_key": "YOUR_API_KEY_HERE",
  "auth_mode": "bearer",
  "model": "claude-sonnet-4.6",
  "max_steps": 10,
  "max_tokens": 4096,
  "timeout_seconds": 3000,
  "shell_timeout": 30
}
```

### 配置项说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `base_url` | API 端点地址 | - |
| `api_key` | API 密钥 | - |
| `auth_mode` | 认证模式：`bearer`、`x-api-key`、`none` | `bearer` |
| `model` | 使用的模型名称 | `claude-sonnet-4.6` |
| `max_steps` | Agent 最大循环步数 | `10` |
| `max_tokens` | 每次请求最大 token 数 | `4096` |
| `timeout_seconds` | HTTP 请求超时时间 | `3000` |
| `shell_timeout` | Shell 命令执行超时 | `30` |

也可以通过环境变量配置：

```bash
export CURSOR2API_BASE_URL="https://your-api-endpoint.com"
export CURSOR2API_API_KEY="your-api-key"
export CURSOR2API_MODEL="claude-sonnet-4.6"
```

## 使用方法

### 交互式会话模式

```bash
python main.py
```

启动后可以持续输入任务，直到输入 `exit` 或 `quit` 退出。

### 单次任务模式

```bash
python main.py --once "读取 workspace/hello.py 文件内容"
```

### 命令行参数

```bash
python main.py [task] [options]

参数:
  task                    要执行的任务

选项:
  --config PATH           配置文件路径
  --base-url URL          API 端点地址
  --api-key KEY           API 密钥
  --model NAME            模型名称
  --max-steps N           最大循环步数
  --max-tokens N          最大 token 数
  --timeout-seconds N     请求超时时间
  --auth-mode MODE        认证模式
  --skip-preflight        跳启动检查
  --once                  单次任务模式
```

### 服务器模式

```bash
python server.py
```

启动 HTTP 服务器，监听配置的端口（默认 9090）。

## 可用工具

Agent 可以使用以下工具：

| 工具 | 说明 | 参数 |
|------|------|------|
| `read_file` | 读取文件内容 | `path`: 文件路径 |
| `write_file` | 写入文件内容 | `path`: 路径, `content`: 内容 |
| `list_dir` | 列出目录内容 | `path`: 目录路径 |
| `run_shell` | 执行 Shell 命令 | `command`: 命令 |

## 安全机制

1. **文件操作限制**：所有文件操作限制在 `workspace/` 目录内
2. **命令白名单**：仅允许执行 `ls`、`python`、`node` 命令
3. **超时保护**：Shell 命令有执行超时限制
4. **步数限制**：防止无限循环的最大步数限制

## 示例

```bash
# 启动交互式会话
python main.py

>>> 读取 hello.py 的内容
[Agent 执行 read_file 工具...]
[返回文件内容]

>>> 创建一个 test.txt 文件，写入 "Hello World"
[Agent 执行 write_file 工具...]
[文件创建成功]

>>> exit
Session closed.
```

## 设计文档

详细的设计思路和架构说明请参考 [DESIGN.md](DESIGN.md)。

## License

MIT
