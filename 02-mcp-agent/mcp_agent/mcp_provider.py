# coding: utf-8
"""MCPToolProvider：连接 MCP Server，动态加载工具，代理调用。

设计取舍：
  - list_tools() 直接返回 OpenAI tools 格式 —— 中间表示层在这个阶段没有价值
  - call_tool() 所有失败降级为字符串 —— 主循环不感知跨进程错误

生命周期：
    async with MCPToolProvider(...) as p:
        tools = p.list_tools()
        result = await p.call_tool("read_file", {"path": "x.txt"})
"""
import asyncio
import time
from contextlib import AsyncExitStack
from typing import Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPToolProvider:
    def __init__(
        self,
        command: str,
        args: list[str],
        server_name: str = "mcp",
        timeout: float = 30.0,
        on_exchange: Callable[[dict], None] | None = None,
    ):
        self._params = StdioServerParameters(command=command, args=args)
        self._server_name = server_name
        self._timeout = timeout
        self._on_exchange = on_exchange

        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._tool_names: set[str] = set()
        self._schemas: list[dict] = []   # 缓存 OpenAI 格式，list_tools 直接返回

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    async def __aenter__(self) -> "MCPToolProvider":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def start(self):
        read, write = await self._stack.enter_async_context(
            stdio_client(self._params)
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()

        resp = await self._session.list_tools()
        self._tool_names = {t.name for t in resp.tools}
        self._schemas = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema,
                },
            }
            for t in resp.tools
        ]

    async def close(self):
        await self._stack.aclose()
        self._session = None

    # ------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------
    def list_tools(self) -> list[dict]:
        """返回 OpenAI 兼容的 tools 参数，直接喂给 LLMClient.call()。"""
        return self._schemas

    async def call_tool(self, name: str, args: dict) -> str:
        if self._session is None:
            return "Error: MCP provider not started"
        if name not in self._tool_names:
            return f"Error: unknown MCP tool '{name}'"

        t0 = time.time()
        error_type = None
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments=args),
                timeout=self._timeout,
            )
            text = self._stringify(result)
        except asyncio.TimeoutError:
            error_type = "timeout"
            text = f"Error calling {name}: timeout after {self._timeout}s"
        except Exception as e:
            error_type = type(e).__name__
            text = f"Error calling {name}: {type(e).__name__}: {e}"

        if self._on_exchange is not None:
            self._on_exchange({
                "ts": time.time(),
                "server": self._server_name,
                "tool": name,
                "args": args,
                "result": text,
                "duration_ms": round((time.time() - t0) * 1000, 1),
                "error_type": error_type,
            })
        return text

    # ------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------
    @staticmethod
    def _stringify(result) -> str:
        """把 MCP 的 content 数组拍平成字符串。

        MCP 返回 content 是 list[ContentBlock]，类型有 text / image / audio / resource。
        这里只处理 text，其余类型打占位符 —— 当前 server 只会返回 text。
        """
        parts = []
        for block in result.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                parts.append(block.text)
            else:
                parts.append(f"[{btype} content]")
        text = "\n".join(parts) if parts else "(empty result)"
        if getattr(result, "isError", False):
            return f"Error from MCP tool: {text}"
        return text