# 02 - MCP Agent

把 Agent 的工具来源，从进程内写死，改成连接 MCP Server 动态加载。

同一个 Agent 循环，工具从"代码里定义"变成"运行时发现"。

> 这是 [agent-learning](../) 系列的第二篇。
> 第一章：[01-minimal-agent](../01-minimal-agent)

## 做了什么

- ✅ **MCP 客户端**：连接 MCP Server、`tools/list` 拉工具、`tools/call` 调用
- ✅ **手写 MCP Server**：把 01 的 3 个工具改造成 MCP 协议 server
- ✅ **失败全降级**：连接断、超时、非法返回，都变成字符串回给 LLM
- ✅ **主循环零改动**：换 server 只改启动命令，循环逻辑不动
- ✅ **MCP 交互落盘**：每次跨进程调用的 args / result / 耗时进 JSONL
- ✅ **差分测量脚本**：量化工具定义本身的 token 成本

## 为什么不抽 ToolProvider 抽象

我一开始写了 `ToolProvider` 接口 + `LocalToolProvider` + `MCPToolProvider`，
后来删掉了。

**只有一个实现时，抽象是负担**。Protocol 是空的、多一层跳转、
读者要多理解一个概念。02 章不做假想的未来扩展。

如果 03 章真的需要第二个 provider，那时候再抽——重构成本很低，
因为只有一个调用点。

**这是一条经验**：01 的 `@tool` 装饰器抽象是对的，因为它确实有 3 个工具；
02 的 `ToolProvider` 抽象是错的，因为它只有一个实现。**经验不能无脑迁移**。

## 目录结构

```
02-mcp-agent/
├── mcp_agent/              # client
│   ├── llm.py              # 从 01 拷贝
│   ├── logger.py           # 从 01 拷贝 + mcp_exchanges 字段
│   ├── main.py             # 主循环（只走 MCP）
│   └── mcp_provider.py     # MCP 客户端封装
├── mcp_server/             # server（自写）
│   └── main.py             # 3 个工具的 MCP 版
├── scripts/
│   └── measure_tool_cost.py    # 工具定义 token 成本测量
├── notes/
│   ├── 01-tool-schema-cost.md      # 工具 schema 的 token 成本
│   └── 02-protocol-wire-format.md  # MCP 协议实测报文
├── workspace/              # 沙箱目录
├── logs/                   # 运行日志（gitignore）
├── pyproject.toml
└── uv.lock
```

## 快速开始

依赖 [uv](https://github.com/astral-sh/uv) 管理。

```bash
git clone https://github.com/jiesunn/agent-learning
cd agent-learning/02-mcp-agent

uv sync
cp .env.example .env      # 填入 API Key
uv run python -m mcp_agent.main
```

client 会自动通过 `subprocess` 启动 `mcp_server/main.py`，无需手动启动。

## 架构

```
你的 Agent（mcp_agent）
    │
    ├── ① OpenAI 协议 ──> LLM（DeepSeek / GLM）
    │     messages + tools
    │
    └── ② MCP 协议 ──> MCP Server（mcp_server）
          JSON-RPC over stdio
```

两条链路独立：

- **①** 是 LLM 侧的 function calling 格式，`tools` 字段是 `{"type": "function", "function": {...}}`
- **②** 是工具侧的 MCP 协议，`inputSchema` 是 camelCase，走 JSON-RPC

`MCPToolProvider` 负责把 ② 拿到的 `inputSchema`，包装成 ① 的格式。
**这两条链路的格式差异，是理解 02 章的关键。**

## 主循环 diff（相对 01）

```diff
- tools = get_tool_schemas()
+ tools = provider.list_tools()

- result = execute_tool(name, args)
+ result = await provider.call_tool(name, args)
```

其余循环结构、消息配对、`finish_reason` 判定、日志逻辑**完全不动**。

## 设计决策

| 决策 | 理由 |
|------|------|
| `list_tools()` 直接返回 OpenAI 格式 | 只有一个 LLM 协议，中间表示层没价值 |
| 失败降级为字符串 | 复用 01 的"异常即字符串"约定，主循环不感知失败源 |
| 主循环改 async | MCP SDK 是 async。01 的同步主循环在这里必须还债 |
| LLM 调用包 `asyncio.to_thread` | `LLMClient` 仍是同步 requests，这是权宜（04 章换 httpx） |
| 工具 schema 循环外算一次 | 01 每轮调 `get_tool_schemas()` 是本地零成本；MCP 每轮是 RPC |
| 用 `Annotated[Field]` 传参数描述 | docstring 的 Args 段 SDK 不解析（详见笔记） |

## 数据

工具定义的 token 成本，两个 provider、两种语言、3 与 14 工具：

| Provider | 语言 | 工具数 | 净成本 | 每工具 |
|----------|------|--------|--------|--------|
| GLM-4.5-Air | 中文 | 3 | 425 | 141.7 |
| GLM-4.5-Air | 英文 | 3 | 422 | 140.7 |
| DeepSeek | 中文 | 3 | 521 | 173.7 |
| DeepSeek | 英文 | 3 | 525 | 175.0 |
| GLM-4.5-Air | 英文 | 14 | 2069 | 147.8 |
| DeepSeek | 英文 | 14 | 2182 | 155.9 |

三条结论：

1. **每工具约 140-175 tokens**，随数量近似线性。挂 10 个工具，
   第一轮 prompt 至少多花 1400+ tokens。
2. **描述语言对成本没影响**（中文 vs 英文差 < 1%）。
   选语言看可读性，不看 token。
3. **token 数不可跨 provider 移植**（同一 schema DeepSeek 比 GLM 多 23%）。
   所有 token 数字必须标 provider。

完整数据和分析见 [`notes/01-tool-schema-cost.md`](./notes/01-tool-schema-cost.md)。

## 踩过的坑

- **`inputSchema` 在 v2 SDK 里改名为 `input_schema`**：类型检查器报"未知属性"，因为 wire 上是 camelCase，Python 侧转 snake_case。
- **docstring 的缩进不会被 dedent**：SDK 用 `fn.__doc__` 而不是 `inspect.getdoc()`，多行 docstring 的 4 空格缩进全部发出，第 1 轮 prompt 涨 70%+。解法是用 `@mcp.tool(description=...)` 显式控制。
- **docstring 的 Args 段 SDK 不解析**：参数描述只能靠 `Annotated[str, Field(description=...)]`。这一点在文档里没写清楚，我是跑了两组数据才确认。
- **stdio 传输下 stdout 是协议专用**：在 server 里 `print()` 调试会污染 JSON-RPC 帧，client 解析失败。
- **`WORKSPACE = Path("./workspace")` 是相对 server 进程的 cwd**：server 由 client 用 subprocess 启动，cwd 是 client 的工作目录。从不同目录启动会指向不同位置。
- **`MCPServer(name)` 默认 `version=""`**：initialize 响应里 `serverInfo.version` 是空的。生产环境应传 `version="0.1.0"`。

## 未做的事

- **`structuredContent` 没用上**：MCP 的 `tools/call` 响应里有 `structuredContent` 字段（对应 `outputSchema`），比 `content` 更适合程序化处理。当前 `_stringify()` 只取了 `content`。
- **失败注入未做**：`FAULT_MODE` 开关设计过但没实现。server 挂 / 超时 / 非法返回三种失败模式的实测日志还没跑。
- **多 server 编排**：当前只连一个 server。多 server 的命名空间、工具冲突、并行调用留给后续章节。
- **HTTP 传输**：当前只用 stdio。Streamable HTTP 是另一种传输，适合 server 独立部署的场景。

## 下一步

`03-multi-agent`：Planner / Executor / Evaluator 协作。

03 章会用到 02 章的两个遗留钩子：

- `structuredContent` —— 让工具返回结构化数据给下游 Agent
- 多 server 连接 —— 不同 Agent 挂不同工具集

## 相关

- [`notes/01-tool-schema-cost.md`](./notes/01-tool-schema-cost.md) —— 工具定义 token 成本实测
- [`notes/02-protocol-wire-format.md`](./notes/02-protocol-wire-format.md) —— MCP 协议手工报文实测
- [01-minimal-agent](../01-minimal-agent) —— 手写 Agent 循环（前置章节）

## License

MIT