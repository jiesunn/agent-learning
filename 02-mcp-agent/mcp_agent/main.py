# coding: utf-8
"""Agent 主循环（02 版）。

与 01 的差异：
  - run_agent 变 async（MCP SDK 是 async）
  - 工具来源从全局注册表改为注入的 ToolProvider
  - JSON 解析从 provider 上移到 main
  - 其余循环结构、消息配对、日志逻辑完全不动
"""
import asyncio
import json
import os
import time

from .llm import get_current_client
from .logger import RunLogger
from .mcp_provider import MCPToolProvider

MAX_ITERATIONS = 10

SYSTEM_PROMPT = (
    "你是一个助手，可以使用工具帮助用户完成任务。"
    "如果需要读文件、写文件或列文件，请调用相应工具。"
    "任务完成后直接回复用户，不要继续调用工具。"
)

DEFAULT_TASK = (
    "请在 workspace 下创建一个 hello.txt，内容是 'Hello Agent'，"
    "如果已经存在就返回文件内容。"
)


def print_messages(messages, title=""):
    print(f"\n{'=' * 50}")
    print(f"📋 messages {title} | 共 {len(messages)} 条")
    print('=' * 50)
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = m.get("content")
        tool_calls = m.get("tool_calls")
        tool_call_id = m.get("tool_call_id")

        content_display = (content[:80] + "...") if content and len(content) > 80 else content
        line = f"[{i}] role={role}"
        if tool_call_id:
            line += f" tool_call_id={tool_call_id}"
        line += f" content={content_display!r}"
        if tool_calls:
            names = [tc["function"]["name"] for tc in tool_calls]
            line += f" tool_calls={names}"
        print(line)
    print('=' * 50)


async def run_agent(user_input: str, provider: MCPToolProvider):
    llm_client = get_current_client()
    tool_schemas = provider.list_tools()

    logger = RunLogger(user_input, model=llm_client.model)
    logger.tool_schemas = tool_schemas
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]

    print_messages(messages, "初始状态")
    print(f"🔧 Provider: {type(provider).__name__} | 工具数: {len(tool_schemas)}")
    for s in provider.list_tools():
        print(f"   - {s['function']['name']} ")

    status = "unknown"
    final_answer = None

    for i in range(MAX_ITERATIONS):
        round_num = i + 1
        print(f"\n--- 第 {round_num} 轮 ---")
        print_messages(messages, f"发给 LLM 前（第 {round_num} 轮）")

        messages_before = [dict(m) for m in messages]
        start = time.time()
        # LLMClient 仍是同步 requests —— 用 to_thread 包一层是 02 章的权宜，
        # 04 章 Agent Toolkit 会换成 httpx async client。
        resp = await asyncio.to_thread(llm_client.call, messages, tool_schemas)
        duration = time.time() - start

        choice = resp.choices[0]
        msg = choice.message

        print(f"finish_reason: {choice.finish_reason}")
        print(f"content: {msg.content}")
        if msg.tool_calls:
            for tc in msg.tool_calls:
                print(f"  → 调用工具: {tc.function.name}({tc.function.arguments})")

        assistant_message = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_message["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        messages.append(assistant_message)
        print_messages(messages, f"追加 assistant 后（第 {round_num} 轮）")

        tool_results = []
        if choice.finish_reason == "tool_calls":
            for tc in msg.tool_calls:
                name = tc.function.name
                # 解析从 provider 上移到 main：GLM 空参可能返回 "" / "{}" / "null"
                raw = tc.function.arguments
                args = json.loads(raw) if raw else {}
                if not isinstance(args, dict):
                    args = {}

                print(f"  执行 {name}...")
                result = await provider.call_tool(name, args)
                print(f"  结果: {result[:200]}{'...' if len(result) > 200 else ''}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
                tool_results.append({
                    "tool_call_id": tc.id,
                    "name": name,
                    "result": result,
                })
            print_messages(messages, f"追加 tool 后（第 {round_num} 轮）")

        logger.log_round(round_num, messages_before, resp, tool_results, duration)

        if choice.finish_reason == "stop":
            print("\n✅ 任务完成")
            final_answer = msg.content
            status = "success"
            break

        if choice.finish_reason == "tool_calls":
            continue

        print(f"\n⚠️ 未预期的 finish_reason: {choice.finish_reason}")
        final_answer = msg.content
        status = f"unexpected_{choice.finish_reason}"
        break
    else:
        print(f"\n⚠️ 达到最大轮数 {MAX_ITERATIONS}，强制退出")
        status = "max_iterations"

    log_file = logger.finish(final_answer, status)
    print(f"\n📝 日志已保存: {log_file}")
    logger.print_summary()

    return final_answer


async def main():
    user_input = os.getenv("AGENT_TASK", DEFAULT_TASK)

    provider = MCPToolProvider(
        command="uv",
        args=["run", "python", "-m", "mcp_server.main"],
        server_name="filesystem-minimal",
        timeout=float(os.getenv("MCP_CALL_TIMEOUT", "30")),
    )
    async with provider:
        await run_agent(user_input, provider)


if __name__ == "__main__":
    asyncio.run(main())