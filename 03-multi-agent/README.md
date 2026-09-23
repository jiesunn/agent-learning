# 03 - Multi-Agent

把"多 Agent 协作"从一个 prompt 套路，变成一个**显式状态机**：
Planner / Executor / Evaluator 三个角色只产出结构化建议，控制流由纯函数裁决。

> 这是 [agent-learning](../) 系列的第三篇。
> 第一篇：[01-minimal-agent](../01-minimal-agent)（手写 Agent 循环）
> 第二篇：[02-mcp-agent](../02-mcp-agent)（MCP 动态加载工具）

## 先说结论

三个任务、两种架构、同一模型、同一套工具、程序化验收产物，结果是：

| 任务 | 单 Agent tokens | 多 Agent tokens | 倍数 | 正确率差异 |
|------|----------------|----------------|------|-----------|
| hello（1 步任务） | 4063 | 16951 | **4.2×** | 无 |
| sales（读 CSV 出报表） | 2805 | 25436 | **9.1×** | 无 |
| requirements（交叉比对冲突来源） | 5940 | 30844 | **5.2×** | 无 |

**多 Agent 不是默认更优解。** 在这三个任务上，它一个错误都没多拦住，纯是净开销。

那这一章的价值在哪？在**另一半**：

- 三个角色里最贵的不是 Planner（只占 3–5%），而是 **Evaluator（41–52%，和 Executor 基本相等）**
  —— 因为独立核验是真花钱的，"便宜的独立验证"不存在。
- 单 Agent 赢的真正原因是**并行工具调用**：它一轮能读 3 个文件、写 2 个产物；
  多 Agent 的串行状态机主动放弃了这一杠杆（同一任务工具调用 6 次 → 29 次）。
- 把控制流写成代码之后，**编排的失败路径变成了可断言的事实**：
  73 个用例离线 0.26 秒跑完，其中 19 个是故障注入，
  覆盖"评估器永远说 revise""Planner 永远返回废话""预算在第 3 次调用爆"这些用真 LLM 几乎无法复现的路径。

完整分析和局限见 [`notes/02-cost-attribution.md`](./notes/02-cost-attribution.md)，
状态机的设计论证见 [`notes/01-why-explicit-state-machine.md`](./notes/01-why-explicit-state-machine.md)。

## 做了什么

- ✅ **显式状态机**：5 个动作（plan/execute/evaluate/finish/abort）+ 3 条不变量，`decide()` 是零依赖纯函数
- ✅ **三个角色，工具集即权限边界**：Planner 无工具、Executor 读写、**Evaluator 只读**（不是"被要求别改"，是"改不了"）
- ✅ **结构化输出**：Planner/Evaluator 全部输出 JSON，三级降级解析 + 一次格式修复
- ✅ **预算守卫**：挂在**每一次 LLM 调用之前**，而不是每轮循环之后
- ✅ **失败路径可断言**：19 个故障注入用例，用 scripted 假客户端把偶发失败变成确定性用例
- ✅ **按角色归因成本**：每次调用打 agent 标签 + 状态转移事件流
- ✅ **单 Agent 基线**：同模型、同工具、同预算、同 logger，唯一变量是控制流
- ✅ **独立产物验收**：程序断言产物内容，与"状态机判定"分开记，两者不一致会显式标出
- ✅ **数据可复算**：`scripts/report.py` 从原始 JSON 生成 README 里所有表格

## 目录结构

```
03-multi-agent/
├── multi_agent/
│   ├── state.py          # 数据模型 + 纯函数状态机（零依赖，可单测）
│   ├── parsing.py        # 模型输出 -> dict 的三级降级解析（零依赖，可单测）
│   ├── tools.py          # 工具注册表 + 按角色裁剪权限
│   ├── llm.py            # LLM 客户端（重试 / 超时 / max_retries=0）
│   ├── logger.py         # 按角色归因 token + 状态转移事件流
│   ├── agents.py         # Planner / Executor / Evaluator
│   ├── orchestrator.py   # 装配 + 主循环 + 保险丝
│   ├── runner.py         # 多 Agent / 单 Agent 两个入口
│   └── main.py           # CLI
├── tests/                # 73 个离线用例（19 个故障注入）
├── scripts/
│   ├── compare.py        # 3 任务 × 2 模式对照实验 + 独立产物验收
│   └── report.py         # 原始 JSON -> Markdown 表格
├── fixtures/             # 实验素材（每个任务的初始 workspace）
├── notes/
│   ├── 01-why-explicit-state-machine.md
│   └── 02-cost-attribution.md
├── workspace/            # 沙箱目录（gitignore）
└── logs/                 # 运行日志（gitignore）
```

## 快速开始

```bash
cd agent-learning/03-multi-agent

uv sync
cp .env.example .env          # 填 API Key

# 跑多 Agent
uv run python -m multi_agent.main

# 跑单 Agent 基线做对照
uv run python -m multi_agent.main --mode solo --task "同一个任务"

# 离线测试，不花额度
uv run pytest

# 完整对照实验（3 任务 × 2 模式，会真实消耗额度）
uv run python scripts/compare.py
uv run python scripts/report.py       # 把原始数据转成 Markdown 表格
```

> `scripts/compare.py` 必须从 `03-multi-agent` 目录执行 ——
> `tools.py` 里的 `WORKSPACE = Path("./workspace")` 是相对 cwd 的。

## 架构

```
                    ┌────────────────────────────────────┐
                    │   decide(state, budget) -> Decision │  <- 纯函数
                    │   PLAN / EXECUTE / EVALUATE         │     无网络、无 LLM
                    │   FINISH / ABORT                    │     可 100% 单测
                    └───────────────┬────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
        ┌───────────┐         ┌───────────┐         ┌─────────────┐
        │  Planner  │         │ Executor  │         │  Evaluator  │
        │  无工具   │         │ 读写工具  │         │  只读工具   │
        └─────┬─────┘         └─────┬─────┘         └──────┬──────┘
              │                     │                      │
           Plan(步骤表)        文件和汇报              Verdict
            建议                 建议                pass/revise/replan
              │                     │                      │
              └─────────────────────┴──────────────────────┘
                                    │
                          写回 RunState，回到 decide()
```

一次成功的运行，事件流长这样：

```
PLAN      -> 生成 4 步计划
EXECUTE   -> step1 attempt1   (executor 2 轮 / 1 次工具调用)
EVALUATE  -> step1 PASS
EXECUTE   -> step2 attempt1
EVALUATE  -> step2 PASS
...
FINISH    -> all_steps_passed
```

`decide()` 的判定顺序（有意为之，见 notes/01）：**预算 → 无计划 → 全部完成 → 重规划 → 待评估 → 重试 → 执行**。

## 设计决策

| 决策 | 理由 |
|------|------|
| 控制流写成纯函数 `decide()`，不写进 prompt | 让编排可单测、可观测、模型无法自己宣布通过 |
| Evaluator 只给只读工具 | 有写权限它就会自己动手改，评估退化成自评自改 |
| `StepStatus` 只有 pending/passed，没有 failed | 失败是待裁决的中间态，不是持久状态；少一个状态少一类分支 |
| 预算守卫挂在每次 LLM 调用**之前** | 挂在每轮之后的话，一个 6 轮工具循环能一口气超预算 6 倍 |
| 执行后 `step.verdict = None` | 产物变了旧评估就作废，避免"用旧评估放行新产物" |
| 评估器输出不可解析时降级为 `revise` | **绝不默认 pass** —— 评估器坏了不能等于放行 |
| 用 `msg.tool_calls` 而不是 `finish_reason` 判断是否调工具 | 有的服务带 tool_calls 时仍返回 `stop`，按枚举值判断会漏（有测试） |
| `max_retries=0` 关掉 SDK 自带重试 | 否则和我们的重试叠乘，最坏 9 次请求，耗时和 token 统计全失真 |
| 主循环单线程同步（02 是 async） | 工具是本地函数、步骤是串行的，这里没有并发可吃；不为统一而统一 |
| 用假 LLM 客户端做故障注入 | 用真模型复现失败路径要么靠运气要么烧钱 |
| 单 Agent 基线放在同一个 `runner.py` | 对比要"同模型同工具同预算"，两份代码各写各的日志就没有可比性 |
| FINISH 时不额外调 LLM 做总结 | 那会引入一次"总结可能失真"，各步产出原文已经在日志里了 |

## 数据

模型 `deepseek-flash`，预算 5 步 / 每步 2 次 / 1 次重规划 / 30 次调用。
由 `uv run python scripts/report.py` 从 `logs/compare_20260923_194900.json` 生成。

### 总览

| 任务 | 模式 | 状态机判定 | 独立产物验收 | LLM 调用 | tokens | 耗时 | 计划步数 |
|------|------|-----------|-------------|---------|--------|------|---------|
| hello | multi | success | PASS | 16 | 16951 | 23.92s | 4 |
| hello | solo | success | PASS | 5 | 4063 | 3.94s | — |
| sales | multi | success | PASS | 22 | 25436 | 22.65s | 4 |
| sales | solo | success | PASS | 3 | 2805 | 2.73s | — |
| requirements | multi | success | PASS | 20 | 30844 | 28.48s | 4 |
| requirements | solo | success | PASS | 3 | 5940 | 7.72s | — |

### 多 Agent 的 token 归因（这一章最核心的一张表）

| 任务 | Planner | Executor | Evaluator | 合计 |
|------|---------|----------|-----------|------|
| hello | 838 (5%) | 7280 (43%) | 8833 (**52%**) | 16951 |
| sales | 947 (4%) | 12679 (50%) | 11810 (46%) | 25436 |
| requirements | 1007 (3%) | 17054 (55%) | 12783 (41%) | 30844 |

### 工具调用次数

| 任务 | 单 Agent | 多 Agent | 单 Agent 调用轮次 | 多 Agent 调用轮次 |
|------|---------|---------|-----------------|-----------------|
| hello | 4 | 10 | 5 | 16 |
| sales | 3 | 19 | 3 | 22 |
| requirements | 6 | **29** | 3 | 20 |

### 五条结论

1. **正确率没有提升，成本 4.2–9.1 倍。** 6 次运行里状态机判定与独立验收完全一致。
   （冒烟测试里还有一次更有意思的结果：状态机报 `failed`（预算烧完），但产物验收 PASS ——
   任务其实做完了。**"状态机说失败"和"任务失败"是两件事**，所以要两套口径。）
2. **Planner 不是成本大头（3–5%）。** 它输入只有目标字符串，是唯一"输入比输出小"的角色。
3. **评估成本 ≈ 执行成本（41–52%）。** 因为 Evaluator 真的在重读产物独立核验。
   `hello` 那次评估比执行还贵。**独立验证是真花钱的。**
4. **每一步至少 4 次 LLM 调用。** Executor 永远"先调工具再汇报"；Evaluator 是"list → read → 判定"。
5. **单 Agent 赢在并行工具调用。** 它一轮能发 `[list_files, read_file × 3]`；
   多 Agent 的串行状态机把同一件事拆成 4 步，每步两个角色各自重读一遍 → 工具调用 6 次涨到 29 次。

### 什么时候才值得拆（假设，本章未验证）

至少要满足一条：工作集超出单窗口舒适区 / 步骤错误会累积 / 子任务可并行 /
权限边界是刚需（生产里"执行 Agent 不能碰生产库"）/ 需要审计轨迹。
一条都不满足时，**单 Agent 更优**。

## 踩过的坑

- **`package = false` 导致 pytest 找不到包**：项目根目录不在 `sys.path` 上，
  4 个测试文件全部 `ModuleNotFoundError: No module named 'multi_agent'`。
  解法是 `[tool.pytest.ini_options]` 里加 `pythonpath = ["."]`。
- **openai SDK 自带 2 次重试，会和我们自己的重试叠乘**：最坏 3×3=9 次请求，
  耗时和 token 统计全失真。必须 `OpenAI(max_retries=0)`，让重试只有一处。
- **`finish_reason == "tool_calls"` 不是可靠的判断依据**：有的服务带 tool_calls 时仍返回 `stop`。
  01/02 的写法会把这批工具调用丢掉，而带 tool_calls 的 assistant 消息已经进了上下文
  —— 下一次请求就出现"有 tool_call 没有对应 tool 结果"的错配。
  现在改成看 `msg.tool_calls` 是否为空，`tests/test_orchestrator_faults.py` 里有这条用例。
- **Planner 会过度拆解**：一个"创建文件"的任务被拆成 4 步（其中一步是"跳过读取"的空操作）。
  多 Agent 花 16 次调用，单 Agent 5 次。计划步数和任务复杂度不挂钩，这是当前 prompt 的缺陷。
- **Evaluator 的最后一次调用没有输出长度约束**：`hello` 那次它为了给一句判定生成了
  **1773 个 completion token**（占该次运行总量的 10.5%），正常只需要几十个。
  加 `max_tokens=200` 就能省掉，但改了要重跑全部数据，所以留作已知问题。
- **状态机也要有保险丝**：`decide()` 是纯函数，理论上不会不收敛；
  但一旦有人改坏它导致"动作不改变状态"，主循环会挂死。
  `max_iterations=60` 兜底，`test_iteration_fuse_prevents_hang` 用 monkeypatch 验证这条路径。
- **`WORKSPACE = Path("./workspace")` 是相对 cwd 的**（从 02 继承的坑）：
  `scripts/compare.py` 用 `ROOT/workspace` 做断言，如果从别的目录启动，
  Agent 会写到 `./workspace`，断言会查另一个目录。必须从章节目录执行。

## 未做的事

- **并行步骤**：状态机是纯串行的，这是当前实现最大的浪费点
  —— 数据里单 Agent 靠并行工具调用赢了，而多 Agent 连"步骤级并行"都没做。
- **Evaluator 输出长度约束**：上面那条 1773 token 的浪费，一行 `max_tokens` 能修，未修。
- **计划步数与任务复杂度自适应**：现在 `max_steps` 是固定值，Planner 对简单任务也拆 4 步。
  应该有"计划复杂度自评 + 简单任务直接单步执行"的短路。
- **成本感知路由**：预算触顶时不是简单 ABORT，而是降级（丢掉 Evaluator、或退化成单 Agent 跑完剩余步骤）。
- **重复读文件的缓存**：Executor 读过的文件 Evaluator 又读一遍，29 次工具调用里相当一部分是重复的。
  一个按 (path, mtime) 做的读缓存能省掉，代价是牺牲一部分"独立核验"的纯度。
- **`structuredContent` 依然没用**：02 章留下的钩子，这一章走本地工具，仍然没接。
- **多 server 连接**：02 章说 03 会做，实际没做 —— 这一章的重点变成了状态机，工具来源保持简单。
- **n=1 的采样**：每个组合只跑一次，token 波动（主要来自输出长度）没有均值化。

## 下一步

`04-agent-toolkit`：把 01–03 里重复出现的东西抽成可复用模块 ——
LLM 客户端（换 httpx async）、按角色的 token 记账、JSON 降级解析、
预算守卫、可注入的假客户端。03 章里"为了可测而写纯函数"的经验，应该固化成工具。

`05-agent-eval`：这一章已经在 `scripts/compare.py` 里手搓了一个最小评估框架
（独立产物验收 + 两套口径对比）。05 章把它做成体系：成功率、成本、延迟、回归。

## 相关

- [`notes/01-why-explicit-state-machine.md`](./notes/01-why-explicit-state-machine.md) —— 控制流为什么不能交给 LLM
- [`notes/02-cost-attribution.md`](./notes/02-cost-attribution.md) —— 3 任务 × 2 模式完整数据与局限
- [02-mcp-agent](../02-mcp-agent) —— MCP 动态加载工具（前置章节）
- [01-minimal-agent](../01-minimal-agent) —— 手写 Agent 循环（前置章节）

## License

MIT
