# coding: utf-8
"""差分测量工具定义的 token 净成本。

原理：
    同一个 messages，发两次请求 —— 一次不带 tools，一次带 tools。
    两次的 prompt_tokens 差 = 工具定义本身的净成本。

    这比看总 prompt_tokens 精确，因为它把 system / user / history 的
    token 成本排除掉了，只留下 tools 的贡献。

用法：
    # 测自写 server（3 工具）
    uv run python scripts/measure_tool_cost.py --server minimal

    # 测官方 filesystem server（14 工具）
    uv run python scripts/measure_tool_cost.py --server filesystem
"""
import argparse
import asyncio
import json
from pathlib import Path

from mcp_agent.llm import get_current_client
from mcp_agent.mcp_provider import MCPToolProvider


# 测量用固定的最小 messages，避免 messages 本身干扰
PROBE_MESSAGES = [
    {"role": "system", "content": "你是一个助手。"},
    {"role": "user", "content": "你好"},
]


async def measure(provider: MCPToolProvider, label: str) -> dict:
    client = get_current_client()
    tools = provider.list_tools()

    # 两次调用：不带 tools / 带 tools
    # 用 to_thread 是因为 LLMClient 是同步 requests 的
    r_no_tools = await asyncio.to_thread(client.call, PROBE_MESSAGES, [])
    r_with_tools = await asyncio.to_thread(client.call, PROBE_MESSAGES, tools)

    baseline = r_no_tools.usage.prompt_tokens
    with_tools = r_with_tools.usage.prompt_tokens
    delta = with_tools - baseline

    # 序列化后的字符数，辅助判断"token 数异常高"是不是因为字面量太大
    chars = len(json.dumps(tools, ensure_ascii=False))

    result = {
        "label": label,
        "num_tools": len(tools),
        "baseline_prompt_tokens": baseline,
        "with_tools_prompt_tokens": with_tools,
        "tools_token_cost": delta,
        "tokens_per_tool": round(delta / len(tools), 1) if tools else 0,
        "tools_json_chars": chars,
        "chars_per_token": round(chars / delta, 2) if delta else 0,
    }

    print(f"\n{'=' * 50}")
    print(f"📊 {label}")
    print('=' * 50)
    print(f"工具数:                {result['num_tools']}")
    print(f"不含 tools prompt:     {baseline}")
    print(f"含 tools prompt:       {with_tools}")
    print(f"工具定义净成本:        {delta} tokens")
    print(f"每个工具平均:          {result['tokens_per_tool']} tokens")
    print(f"schema JSON 字符数:    {chars}")
    print(f"字符 / token:          {result['chars_per_token']}")
    print('=' * 50)

    return result


async def run_minimal():
    server_path = Path(__file__).resolve().parent.parent / "mcp_server"
    provider = MCPToolProvider(
        command="uv",
        args=["run", "python", "-m", "mcp_server.main"],
        server_name="minimal",
    )
    async with provider:
        return await measure(provider, "自写 server（3 工具）")


async def run_filesystem():
    workspace = Path(__file__).resolve().parent.parent / "workspace"
    provider = MCPToolProvider(
        command="npx",
        args=[
            "-y",
            "@modelcontextprotocol/server-filesystem",
            str(workspace),
        ],
        server_name="filesystem",
    )
    async with provider:
        return await measure(provider, "官方 filesystem server（14 工具）")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--server",
        choices=["minimal", "filesystem", "both"],
        default="both",
        help="测哪个 server",
    )
    args = parser.parse_args()

    results = []
    if args.server in ("minimal", "both"):
        results.append(await run_minimal())
    if args.server in ("filesystem", "both"):
        results.append(await run_filesystem())

    if len(results) == 2:
        a, b = results
        ratio = b["tools_token_cost"] / a["tools_token_cost"] if a["tools_token_cost"] else 0
        print(f"\n{'=' * 50}")
        print("📈 对比")
        print('=' * 50)
        print(f"3 工具 → 14 工具，工具数涨 {b['num_tools'] / a['num_tools']:.2f}x")
        print(f"工具成本涨 {ratio:.2f}x")
        if ratio > 0:
            if ratio > b["num_tools"] / a["num_tools"]:
                print("⚠️  成本增长快于工具数 —— 大 schema 被过度放大")
            else:
                print("✓  成本增长慢于工具数 —— 有规模效应")
        print('=' * 50)

    # 顺手落盘，方便贴进 notes
    out = Path(__file__).resolve().parent.parent / "notes" / "tool-cost-data.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📝 已保存: {out}")


if __name__ == "__main__":
    asyncio.run(main())