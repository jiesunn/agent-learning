# coding: utf-8
"""CLI 入口。

    uv run python -m multi_agent.main
    uv run python -m multi_agent.main --task "..." --max-steps 4
    uv run python -m multi_agent.main --mode solo      # 跑单 Agent 基线做对照
"""
from __future__ import annotations

import argparse
import os

from .runner import run_multi, run_solo
from .state import Budget
from .tools import WORKSPACE

DEFAULT_TASK = (
    "在 workspace 下创建 hello.txt，内容为 'Hello Agent'；"
    "如果文件已存在，就把它的内容原样保留并在末尾追加一行 '-- checked'。"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="03-multi-agent runner")
    parser.add_argument("--task", default=os.getenv("AGENT_TASK", DEFAULT_TASK))
    parser.add_argument("--mode", choices=["multi", "solo"], default="multi")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--max-attempts", type=int, default=2, help="每个步骤最多执行几次")
    parser.add_argument("--max-replans", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=40)
    parser.add_argument("--token-budget", type=int, default=300_000)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    budget = Budget(
        max_steps=args.max_steps,
        max_attempts_per_step=args.max_attempts,
        max_replans=args.max_replans,
        max_llm_calls=args.max_calls,
        max_total_tokens=args.token_budget,
    )

    WORKSPACE.mkdir(exist_ok=True)

    print("=" * 62)
    print(f"🎯 目标: {args.task}")
    print(f"🧩 模式: {args.mode}")
    print(
        f"📦 预算: {budget.max_steps} 步 / 每步 {budget.max_attempts_per_step} 次 / "
        f"{budget.max_replans} 次重规划 / {budget.max_llm_calls} 次调用 / "
        f"{budget.max_total_tokens} tokens"
    )
    print("=" * 62)

    run = run_multi if args.mode == "multi" else run_solo
    result = run(args.task, budget=budget, verbose=not args.quiet)

    print(f"\n{'=' * 62}")
    print(f"🏁 状态: {result.status}")
    if result.state is not None:
        print(f"🔁 重规划次数: {result.state.replans}")
        if result.state.abort_reason:
            print(f"⛔ 终止原因: {result.state.abort_reason}")

    print(f"\n📄 交付内容:\n{result.final_answer or '(无)'}")

    artifacts = sorted(
        str(p.relative_to(WORKSPACE)) for p in WORKSPACE.rglob("*") if p.is_file()
    )
    print(f"\n📁 workspace 产物: {artifacts or '(空)'}")
    print(f"\n📝 日志已保存: {result.log_file}")

    result.logger.print_summary()


if __name__ == "__main__":
    main()
