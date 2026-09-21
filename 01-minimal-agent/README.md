# 01 - Minimal Agent

一个不依赖任何 Agent 框架、从零手写的 Agent 循环。
用最少的代码，把 Agent 的核心机制跑通：任务循环、工具调用、上下文管理、失败处理。

> 这是 [agent-learning](../) 系列的第一章。目标不是"再写一个 Agent"，而是**理解 Agent 到底怎么运转的**。

## 为什么要手写

框架（LangChain / CrewAI / LangGraph）能帮你省事，但会掩盖 Agent 的三个核心问题：

1. LLM 每轮怎么知道"现在该做什么"
2. 工具调用的请求和响应怎么配对
3. 任务什么时候算"完成"

不亲手写一遍，这三个问题永远是黑盒。手写一次，之后用任何框架都能看得清。

## 做了什么

- ✅ **手写 Agent 循环**：以 `finish_reason` 驱动的状态机，无任何 Agent 框架
- ✅ **工具注册机制**：`@tool` 装饰器 + 自动 schema 生成，加工具不改主循环
- ✅ **Function Calling**：兼容 OpenAI 协议的 tool_calls，处理请求-响应配对
- ✅ **异常容错**：工具报错不崩溃，作为字符串回传给 LLM 自行决策
- ✅ **多 LLM 支持**：DeepSeek / GLM 切换只改环境变量，代码零改动
- ✅ **运行日志**：每轮完整落盘为 JSON，含 token 消耗与耗时
- ✅ **Token 统计**：每轮 prompt / completion / total 记录，可观察成本增长

## 目录结构

```
01-minimal-agent/
├── minimal_agent/
│   ├── llm.py          # LLM 客户端封装（多 provider）
│   ├── tools.py        # 工具注册机制 + 3 个内置工具
│   ├── logger.py       # 运行日志 + token 统计
│   └── main.py         # Agent 主循环 + 入口
├── notes/
│   └── model-comparison.md    # DeepSeek vs GLM 行为差异
├── workspace/          # 工具操作的沙箱目录
├── logs/               # 每次运行的 JSON 日志（gitignore）
├── pyproject.toml
└── uv.lock
```

## 快速开始

依赖 [uv](https://github.com/astral-sh/uv) 管理。

```bash
# 克隆
git clone https://github.com/jiesunn/agent-learning
cd agent-learning/01-minimal-agent

# 同步依赖
uv sync

# 配置 API Key
cp .env.example .env
# 编辑 .env，填入 GLM 或 DeepSeek 的 Key

# 运行
uv run python -m minimal_agent.main
```

切换模型只需改 `LLM_NAME`。

## 内置工具

| 工具 | 说明 |
|------|------|
| `read_file` | 读取 workspace 下的文件 |
| `write_file` | 写入 workspace 下的文件 |
| `list_files` | 列出 workspace 下的所有文件 |

所有文件操作限制在 `workspace/` 沙箱目录内，越界直接拒绝。

## Agent 循环长什么样

```
messages = [system, user]

loop:
    resp = LLM(messages, tools)
    messages.append(assistant)

    if finish_reason == "stop":
        return content          # 任务完成

    if finish_reason == "tool_calls":
        for each tool_call:
            result = execute(name, args)
            messages.append(tool, tool_call_id, result)
        continue                # 下一轮
```

核心是三步：
1. **追加 assistant 消息**（含它请求的 tool_calls）
2. **执行工具，把结果作为 tool 消息追加**
3. **判断 `finish_reason`**：`stop` 结束，`tool_calls` 继续

## 设计决策

| 决策 | 理由 |
|------|------|
| 装饰器注册工具 | 加工具只改一处，主循环不动（开闭原则） |
| 工具异常返回字符串 | 让 LLM 看到错误并自主决策，而非程序崩溃 |
| 用 `finish_reason` 驱动循环 | 最朴素的 Agent 状态机，无魔法 |
| `MAX_ITERATIONS = 10` | 硬上限防止死循环烧额度 |
| LLM 封装在 `LLMClient` | 隔离 provider 差异，切模型零成本 |
| 每轮追加 assistant 到历史 | LLM 无状态，必须靠 messages 携带上下文 |

## 观察到的现象

**1. 每轮 prompt_tokens 递增**

同一次 3 轮运行：

| 轮次 | prompt_tokens | completion_tokens |
|------|--------------|------------------|
| 1 | 365 | 98 |
| 2 | 404 | 29 |
| 3 | 431 | 43 |

因为历史累积，每轮都要把完整 messages 重传给 LLM。**Agent 成本随轮数非线性增长**，这是 `02-mcp-agent` 之后要解决的核心问题。

**2. DeepSeek 和 GLM 的行为差异**

| 维度 | DeepSeek | GLM |
|------|----------|-----|
| tool_call_id 格式 | `call_00_xxx` | `call_-724398...`（带负数） |
| tool_calls 时 content | `None` / `''` | 有实际文字（自言自语） |
| content 换行 | 干净 | 前后有 `\n` |

详细记录见 [`notes/model-comparison.md`](./notes/model-comparison.md)。

**结论**：永远不要硬编码 `tool_call_id` 的格式，永远不要假设 `content` 只有一种状态。

## 踩过的坑

- **`uv sync` 报 "Expected a Python module"**：`01_minimal_agent` 以数字开头，不是合法 Python 包名。改用扁平布局 + `[tool.uv] package = false`。
- **`role: 'tool'` 报 400 错误**：漏追加 assistant 消息。tool 消息必须和前面的 assistant 的 tool_calls 配对。
- **相对导入报错**：扁平布局下必须用 `python -m minimal_agent.main` 运行，不能 `python minimal_agent/main.py`。

## 下一步

`02-mcp-agent`：把工具从本地注册改为 MCP 协议动态加载。同一个 Agent 循环，工具来源从"代码里写死"变成"连接 MCP Server 获取"。

## 相关

- [notes/model-comparison.md](./notes/model-comparison.md) — DeepSeek vs GLM 行为差异记录

## License

MIT