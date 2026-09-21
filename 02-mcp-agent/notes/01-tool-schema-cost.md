# 工具 schema 的 token 成本

工具定义是 Agent 每轮 prompt 里最容易被忽视的成本。这篇用两组实验量化它：
一组看"怎么写更省"，一组测"工具定义本身占多少"。

## 实验设计

两个互补的视角：

- **总 prompt 视角**：同一任务下，看不同写法的 `prompt_tokens` 差异。
  包含 system + user + history + tools 的总和。
- **净 tools 视角**：固定 messages，用差分法只测 tools 的贡献。

第一个视角回答"改写法能省多少"，第二个回答"工具定义本身多贵"。两者配合，
才能看清成本结构。

---

## 实验一：三种写法的总 prompt 对比

### 设置

任务固定：`请在 workspace 下创建一个 hello.txt，内容是 'Hello Agent'，如果已经存在就返回文件内容。`

模型固定：glm-4.5-air。

三个版本功能完全一样（read / write / list），只有描述来源不同：

- **A. 本地手写**（01 章）：`@tool(name=..., description=..., parameters={...})`，schema 手写
- **B. MCP + docstring**：`@mcp.tool()`，description 从多行 docstring 生成
- **C. MCP + 显式 description**：`@mcp.tool(description=...)` + `Annotated[str, Field(description=...)]`

### 数据

| 版本 | 第 1 轮 prompt | 总 prompt | 参数描述 | schema 额外字段 |
|------|--------------|----------|---------|----------------|
| A 本地手写 | 365 | 1202 | ✅ | 无 |
| B MCP + docstring | 632 | 2003 | ❌ | `title` |
| C MCP + 显式 description | 492 | 1585 | ✅ | `title` |

日志：
- A: `logs/20260921_220415.json`
- B: `logs/20260921_215926.json`
- C: `logs/20260921_220134.json`

### 观察

#### 1. docstring 是 token 陷阱

B 比 A 第 1 轮多 267 tokens（+73%）。原因不是"MCP 更贵"，是"docstring 写法把空白字符和模板段落全发出去了"。

看 B 的 `tools_sent_to_llm` 里 `read_file.description` 原文：

```
读取 workspace 目录下的文件内容。\n\n    用于查看...\n    不会抛异常。\n\n    Args:\n        path: ...\n\n    Returns:\n        ...
```

- 每行保留 4 空格缩进
- Args / Returns 段 LLM 完全不需要
- 但全都计入 token

#### 2. SDK 不解析 Args 段

B 版本里 `parameters.properties.path`：

```json
{"title": "Path", "type": "string"}
```

没有 `description` 字段。docstring 的 Args 段**没有**被 SDK 转成参数描述。

#### 3. 参数语义只能靠 Annotated + Field

C 版本用：

```python
path: Annotated[str, Field(description="相对于 workspace 的文件路径")]
```

生成的 schema：

```json
{
  "description": "相对于 workspace 的文件路径",
  "title": "Path",
  "type": "string"
}
```

✅ 参数描述到位。

#### 4. MCP 天然带 title 字段

对比 A 和 C 的参数 schema：

A（手写）：
```json
{"type": "string", "description": "相对路径，如 'notes.txt'"}
```

C（MCP 生成）：
```json
{"type": "string", "description": "...", "title": "Path"}
```

`title` 是 Pydantic 自动加的，不是你的代码。每个参数一个，每个工具再加一个 `read_fileArguments` 级别的 title。

**成本**：3 个工具约 40-50 tokens。14 个工具放大 5 倍。

---

## 实验二：差分测工具定义净成本

### 方法

实验一只能看到"总 prompt"的变化。要精确测工具定义本身多贵，需要把
system / user / history 排除掉，只留 tools 的贡献。

差分法：

```python
PROBE_MESSAGES = [
    {"role": "system", "content": "你是一个助手。"},
    {"role": "user", "content": "你好"},
]

r_no_tools = client.call(PROBE_MESSAGES, tools=[])
r_with_tools = client.call(PROBE_MESSAGES, tools=tool_schemas)

tools_cost = r_with_tools.usage.prompt_tokens - r_no_tools.usage.prompt_tokens
```

脚本：`scripts/measure_tool_cost.py`

### 数据

两个 provider × 两种语言 × 3 工具，加两组 14 工具：

| Provider | 描述语言 | 工具数 | 净成本 | 每工具 | chars/token | baseline |
|----------|---------|--------|--------|--------|-------------|----------|
| GLM-4.5-Air | 中文 | 3 | 425 | 141.7 | 2.44 | 12 |
| GLM-4.5-Air | 英文 | 3 | 422 | 140.7 | 3.28 | 12 |
| DeepSeek | 中文 | 3 | 521 | 173.7 | 1.99 | 35 |
| DeepSeek | 英文 | 3 | 525 | 175.0 | 2.64 | 35 |
| GLM-4.5-Air | 英文 | 14 | 2069 | 147.8 | 4.23 | 12 |
| DeepSeek | 英文 | 14 | 2182 | 155.9 | 4.01 | 35 |

3 工具 schema 是自写 server（read / write / list），
14 工具是官方 `@modelcontextprotocol/server-filesystem`。

### 观察

#### 1. 描述语言对 token 成本没有显著影响

同 3 工具、同函数体，只改描述语言：

| Provider | 中文 | 英文 | 差异 |
|----------|------|------|------|
| GLM-4.5-Air | 425 | 422 | -0.7% |
| DeepSeek | 521 | 525 | +0.8% |

中文每字占的 token 更多（chars/token 2.44 vs 3.28），
但表达同样意思用的字更少。两个因素抵消，净成本持平。

**实践含义**：工具描述用中文或英文，对成本没影响。
选择语言应该基于可读性和团队习惯，不必为了省 token 改英文。

#### 2. token 数不可跨 provider 移植

同一个 3 工具 schema：

| | GLM | DeepSeek | 差 |
|---|---|---|---|
| 中文 schema | 425 | 521 | +23% |
| 英文 schema | 422 | 525 | +24% |
| baseline（同句中文） | 12 | 35 | **+192%** |

DeepSeek 的中文 tokenizer 明显更激进，短句尤甚。

**实践含义**：notes 和 README 里的 token 数字必须标注 provider。
不要写"每工具 145 tokens"，要写"每工具 142-175 tokens，取决于 provider"。
选模型不能只看单价——单价低但 token 数高，净成本未必便宜。

#### 3. 每工具成本约 140-175 tokens，近似线性

| Provider | 每工具平均 |
|----------|-----------|
| GLM-4.5-Air | 141-148 |
| DeepSeek | 156-175 |

3 工具和 14 工具的每工具成本接近，说明工具定义成本随数量近似线性增长，
没有规模效应。

**实践含义**：Agent 每挂 10 个工具，第一轮 prompt 至少多花 1400-1750 tokens。
"工具全量注册"在生产里不可持续——挂 50 个工具直接吃掉 7000+ tokens。

#### 4. 一个反例：我错过一次

第一次跑完实验一后，我看到：

| 组 | chars/token |
|----|-------------|
| 自写 server（中文，3 工具） | 2.44 |
| 官方 filesystem（英文，14 工具） | 4.23 |

**当时推断"中文比英文贵 1.7 倍"**，准备写进 notes。

后来做真正的对照实验（同 schema 换语言）才发现：**语言对 token 成本没有影响**。

那两组数字的差异，来自工具数、描述长度、作者都不同——典型的混淆变量。
我把 schema 差异全归给了语言。

**教训**：看到两组数不一样时，先问"是不是还有别的变量在动"。
如果不是严格对照实验，数字之间的因果推断就是猜。

---

## 结论

1. **docstring 是 token 陷阱**：多行 docstring 会把缩进和无用段落全发出去，
   第 1 轮 prompt 涨 70%+。用 `@mcp.tool(description=...)` 显式控制。

2. **参数语义只能靠 `Annotated[Field]`**：docstring 的 Args 段 SDK 不解析，
   不写 `Field(description=...)` 参数就没有描述。

3. **MCP 天然带 `title` 字段**：Pydantic 自动加，约 40-50 tokens / 3 工具。
   本地手写 schema 没有这个开销。

4. **描述语言对成本没影响**：中文和英文差 < 1%。选语言看可读性，不看 token。

5. **token 数不可跨 provider 移植**：同一 schema，DeepSeek 比 GLM 多 23%。
   所有 token 数字必须标 provider。

6. **每工具成本约 140-175 tokens**：与语言无关，与 provider 弱相关。
   Agent 每挂 10 个工具，第一轮多花 1400+ tokens。

7. **MCP 不天然省 token**：本地手写用精简 schema 反而最省。
   MCP 的收益是"工具不写死在代码里"，不是"token 更少"。

---

## 相关

- 实验脚本：`scripts/measure_tool_cost.py`
- 自写 server：`mcp_server/main.py`