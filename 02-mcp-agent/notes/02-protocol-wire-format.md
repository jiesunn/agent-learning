# MCP 协议长什么样

SDK 把 MCP 封装得很好用，但也好到让人看不清"到底传了什么"。
这篇直接手工发 JSON-RPC 报文给 server，看它原样回什么。

看完这篇，MCP 的 client 实现对你来说就没有黑盒了。

## 方法

不经过 client，直接把报文喂给 server 的 stdin：

```bash
cd 02-mcp-agent

# 1. 握手 + 拉工具列表
(
  echo '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"manual","version":"0.1"}}}'
  echo '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  sleep 1
) | uv run python -m mcp_server.main

# 2. 调用工具
(
  echo '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"manual","version":"0.1"}}}'
  echo '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_files","arguments":{}}}'
  sleep 1
) | uv run python -m mcp_server.main
```

---

## 实测报文

### 1. server 响应 initialize

```json
{
  "jsonrpc": "2.0",
  "id": 0,
  "result": {
    "capabilities": {
      "experimental": {},
      "prompts": {"listChanged": false},
      "resources": {"listChanged": false, "subscribe": false},
      "tools": {"listChanged": false}
    },
    "protocolVersion": "2025-06-18",
    "serverInfo": {"name": "filesystem-minimal", "version": ""}
  }
}
```

### 2. server 响应 tools/list

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "read_file",
        "description": "读取 workspace 目录下的文件内容。文件不存在时返回 'File not found: <path>'，不会抛异常。",
        "inputSchema": {
          "properties": {
            "path": {
              "description": "相对于 workspace 的文件路径，如 'notes.txt'。不要传绝对路径。",
              "title": "Path",
              "type": "string"
            }
          },
          "required": ["path"],
          "type": "object",
          "title": "read_fileArguments"
        },
        "outputSchema": {
          "properties": {
            "result": {"title": "Result", "type": "string"}
          },
          "required": ["result"],
          "type": "object",
          "title": "read_fileOutput"
        }
      },
      {
        "name": "write_file",
        "description": "把内容覆盖写入 workspace 下的文件。父目录不存在会自动创建；目标文件已存在会完全覆盖。要追加内容，请先 read_file 再 write_file 拼接。",
        "inputSchema": {
          "properties": {
            "path": {
              "description": "相对于 workspace 的文件路径，如 'notes.txt'。",
              "title": "Path",
              "type": "string"
            },
            "content": {
              "description": "要写入的完整文本内容（覆盖写入，不是追加）。",
              "title": "Content",
              "type": "string"
            }
          },
          "required": ["path", "content"],
          "type": "object",
          "title": "write_fileArguments"
        },
        "outputSchema": {
          "properties": {
            "result": {"title": "Result", "type": "string"}
          },
          "required": ["result"],
          "type": "object",
          "title": "write_fileOutput"
        }
      },
      {
        "name": "list_files",
        "description": "递归列出 workspace 目录下的所有文件（含子目录）。每行一个相对路径。常用于 read_file 前确认文件是否存在。",
        "inputSchema": {
          "properties": {},
          "type": "object",
          "title": "list_filesArguments"
        },
        "outputSchema": {
          "properties": {
            "result": {"title": "Result", "type": "string"}
          },
          "required": ["result"],
          "type": "object",
          "title": "list_filesOutput"
        }
      }
    ]
  }
}
```

### 3. server 响应 tools/call

请求：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "list_files",
    "arguments": {}
  }
}
```

响应：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {"text": "hello.txt", "type": "text"}
    ],
    "isError": false,
    "structuredContent": {"result": "hello.txt"}
  }
}
```

---

## 观察

### 1. stdio 传输 = 换行分隔的 JSON-RPC

没有魔法。JSON 一行行灌进子进程的 stdin，输出从 stdout 一行行读。
没有 HTTP、没有 SSE、没有 length-prefix、没有二进制帧。

`mcp.run(transport="stdio")` 做的事就是：读 stdin 的每一行，解析为
JSON-RPC message，路由到对应的 handler，把结果写回 stdout。

一个副作用：**stdout 是协议专用**。你在 server 里 `print()` 会污染帧，
client 会解析失败。要调试请用 stderr 或 logging。

### 2. request 有 id，notification 没有

- `initialize` 带 `"id": 0` → 需要响应
- `notifications/initialized` 没有 id → 不需要响应

这是 JSON-RPC 2.0 的约定。SDK 在 `session.initialize()` 里自动发了
`notifications/initialized`，所以上层代码看不到这个动作。

### 3. wire 上是 camelCase，SDK 转成 snake_case

协议原文（wire）：

```json
"inputSchema": {...}
"outputSchema": {...}
```

Python SDK 里访问：

```python
t.input_schema     # snake_case
t.output_schema    # snake_case
```

**这是 v2 和 v1 的主要差异之一**。手写 client 时要注意——
如果你的 client 直接读 wire，字段名是 camelCase。

### 4. outputSchema 是 SDK 自动生成的，但不进 prompt

你的函数写成 `def read_file(path: str) -> str`，SDK 不仅从参数生成了
`inputSchema`，还从返回类型 `-> str` 生成了 `outputSchema`。

**这个 outputSchema 只在 wire 上有，不会传给 LLM**。

你的 `MCPToolProvider.list_tools()` 只取了 `t.input_schema`：

```python
{
  "type": "function",
  "function": {
    "name": t.name,
    "description": t.description or "",
    "parameters": t.input_schema,     # 只用 inputSchema
  },
}
```

`outputSchema` 被丢掉了。所以它不占 prompt token——
**wire 上有的东西不等于 prompt 里有的东西**。

代价是：client 不知道工具返回什么类型，只能当字符串处理。
这是当前实现的取舍，也是后续章节可以优化的点。

### 5. capabilities 是双向能力协商

server 在 initialize 响应里声明的能力：

```json
"capabilities": {
  "experimental": {},
  "prompts": {"listChanged": false},
  "resources": {"listChanged": false, "subscribe": false},
  "tools": {"listChanged": false}
}
```

**`tools` 能力存在**，所以 client 才能调 `tools/list` 和 `tools/call`。

如果 server 只声明 `prompts` 不声明 `tools`，client 调 `tools/list`
会被拒绝——这是 capability 协商的意义。

`listChanged: false` 表示工具列表不会动态变化，client 不需要监听变更通知。
如果未来 server 支持热加载工具，这个值要改成 `true`，client 才能感知。

### 6. `serverInfo.version` 是空的

```json
"serverInfo": {"name": "filesystem-minimal", "version": ""}
```

原因：`MCPServer("filesystem-minimal")` 只传了名字，没传版本号。
SDK 默认版本是空字符串。

官方 server 会在启动参数里写版本（如 `"version": "0.6.2"`），你的没有。
**生产环境应该填**——client 判断 server 版本兼容性时会用。

修法：

```python
mcp = MCPServer("filesystem-minimal", version="0.1.0")
```

### 7. `content` 是数组，不是字符串

工具返回 `"hello.txt"`，协议传回来是：

```json
"content": [{"type": "text", "text": "hello.txt"}]
```

**不是** `"content": "hello.txt"`。

这是 MCP 的 ContentBlock 设计——一个工具可以返回多个块（文字 + 图片 + 资源引用混合）。
client 必须遍历数组拼字符串。

`_stringify()` 干的就是这件事：

```python
for block in result.content:
    if btype == "text":
        parts.append(block.text)
```

如果没有这层处理，LLM 收到的是 `[{'type': 'text', 'text': 'hello.txt'}]` 的字符串——
信息没错但难看，还可能误导 LLM。

### 8. `structuredContent` 对应 `outputSchema`

响应里除了 `content`，还多了：

```json
"structuredContent": {"result": "hello.txt"}
```

这是 `tools/list` 里声明的 `outputSchema` 对应的实际值。

**这是 MCP 相对 OpenAI function calling 的增量**：工具能返回结构化数据，
不只是文本。对于需要程序化处理的返回值（比如返回 JSON 对象），
`structuredContent` 比 `content` 更可靠。

**但**：当前 `MCPToolProvider._stringify()` **没处理 `structuredContent`**，
只取了 `content` 数组里的 text。本例中两者碰巧一样（都是 `"hello.txt"`），
但工具返回复杂结构时会丢信息。

这是 03 章可优化的点：把 `structuredContent` 也传给 LLM，或者用它做后续处理。

---

## 一句话总结

MCP 就是 JSON-RPC 2.0 over stdio。
SDK 在协议层上做了很多自动化（schema 生成、能力协商、snake_case 转换），
但底层就是"一行一个 JSON"。

看懂这一点后，MCP 就没有黑盒了。

---

## 未做的事

- [x] ~~手工发 `initialize` 报文~~
- [x] ~~手工发 `tools/list` 报文~~
- [x] ~~手工发 `tools/call` 报文~~
- [ ] 用 official inspector 连一次，对比看到的报文
- [ ] 测未知工具名时 server 返回什么（错误码、错误消息）
- [ ] 测 server 中途挂掉时 client 收到什么
- [ ] 测 `structuredContent` 返回复杂结构（JSON 对象）时 client 怎么处理

---

## 相关

- 客户端代码：`mcp_agent/mcp_provider.py`
- 服务端代码：`mcp_server/main.py`
- 工具成本实验：`notes/01-tool-schema-cost.md`
- MCP 官方规范：https://modelcontextprotocol.io/specification