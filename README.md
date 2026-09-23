# Agent Learning

一个后端工程师转向 Agentic AI 的实战记录。
每一章都是一个独立可运行的项目，从手写 Agent 到 MCP、多 Agent 协作。

不是教程合集，也不是 Demo 坟场。
是一个能跑、能读、能上生产的 Agent 实验室。

## 为什么有这个仓库

我做了 6 年后端（Python / Go，偏业务开发），2025 年开始转 Agentic AI。

转的过程里我发现两件事：

1. **市面上不缺 Agent 教程，缺的是"真实工程实践"**。大部分内容停在"调 API + 跑通 Demo"，没人讲上下文怎么管、失败怎么处理、成本怎么控。
2. **后端工程师转 Agent 有独特优势**：能写生产级代码、能做架构、能处理并发和失败。这些恰恰是 Agent 从 Demo 走向生产最缺的东西。

这个仓库记录我每一章的产出：设计决策、踩坑、数据观察、可运行的代码。

## 章节

| 编号 | 主题 | 状态 | 核心内容 |
|------|------|------|---------|
| [01](./01-minimal-agent) | Minimal Agent | ✅ 完成 | 手写 Agent 循环、工具注册、多 LLM、运行日志 |
| [02](./02-mcp-agent) | MCP Agent | ✅ 完成 | 用 MCP 协议动态加载工具，替代本地写死 |
| [03](./03-multi-agent) | Multi-Agent | ✅ 完成 | 显式状态机、Planner/Executor/Evaluator、单多 Agent 成本实测 |
| 04 | Agent Toolkit | 📅 计划 | 可复用的 Agent 工程模块 |
| 05 | Agent Eval | 📅 计划 | 评估体系：成功率、成本、延迟 |

每个章节**独立可运行**，有独立的 README、依赖、`.env`。

## 技术栈

- **语言**：Python（主力）、Go（性能敏感场景）
- **LLM**：DeepSeek / GLM / Qwen（OpenAI 兼容协议）
- **框架**：不依赖 Agent 框架，手写为主；后续章节评估 LangGraph
- **协议**：MCP（Model Context Protocol）
- **服务**：FastAPI
- **部署**：Serverless（Vercel / AWS Lambda）
- **包管理**：uv

## 快速开始

每个章节独立，进入对应目录按 README 操作。

以 01 为例：

```bash
git clone https://github.com/<your-name>/agent-learning
cd agent-learning/01-minimal-agent

uv sync
cp .env.example .env       # 填入 API Key
uv run python -m minimal_agent.main
```

## 贯穿全系列的设计原则

1. **不装懂**：没做过的事不写进 README
2. **数据说话**：token 消耗、延迟、成功率，都用真实数据
3. **工程视角**：不只讲"能跑"，更讲"怎么上生产"
4. **失败优先**：异常、重试、降级、成本控制，是设计的第一优先级
5. **可观测**：每次运行都有日志、有指标、可复现

## 每章产出物

每完成一章，产出这三样：

- **可运行的代码**：clone 下来就能跑
- **README**：讲清楚设计决策和踩坑
- **notes/**：开发过程中的原始观察

## 关于我

6 年后端工程师（Python / Go），专注 Agentic AI 应用落地。
目标是做出能上生产的 Agent 系统，而不是 Demo。

- GitHub：[@jiesunn](https://github.com/jiesunn)
- 博客：[JieSunn - BLOG](https://jiesunn.github.io/)

## License

MIT