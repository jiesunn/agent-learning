# coding: utf-8
"""把 compare.py 产出的原始 JSON 转成 Markdown 表格。

README 里的每个数字都由这个脚本生成，不手抄 —— 手抄的数字迟早会和代码对不上。

    uv run python scripts/report.py                 # 用最新一次 compare_*.json
    uv run python scripts/report.py logs/compare_xxx.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"


def load(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    files = sorted(LOG_DIR.glob("compare_*.json"))
    if not files:
        sys.exit("logs/ 下没有 compare_*.json，先跑 scripts/compare.py")
    print(f"<!-- 数据来源: {files[-1].name} -->\n")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def table_main(records: list[dict]) -> str:
    lines = [
        "| 任务 | 模式 | 状态机判定 | 独立产物验收 | LLM 调用 | tokens | 耗时 | 计划步数 |",
        "|------|------|-----------|-------------|---------|--------|------|---------|",
    ]
    for r in records:
        steps = r["num_steps"] if r["num_steps"] else "—"
        lines.append(
            f"| {r['task']} | {r['mode']} | {r['agent_status']} "
            f"| {'PASS' if r['artifact_ok'] else 'FAIL'} "
            f"| {r['llm_calls']} | {r['total_tokens']} | {r['wall_seconds']}s | {steps} |"
        )
    return "\n".join(lines)


def table_multiplier(records: list[dict]) -> str:
    by_task: dict[str, dict[str, dict]] = {}
    for r in records:
        by_task.setdefault(r["task"], {})[r["mode"]] = r

    lines = [
        "| 任务 | 单 Agent tokens | 多 Agent tokens | 倍数 | 单 Agent 耗时 | 多 Agent 耗时 | 倍数 |",
        "|------|----------------|----------------|------|--------------|--------------|------|",
    ]
    for task, modes in by_task.items():
        if "solo" not in modes or "multi" not in modes:
            continue
        s, m = modes["solo"], modes["multi"]
        lines.append(
            f"| {task} | {s['total_tokens']} | {m['total_tokens']} "
            f"| **{m['total_tokens'] / s['total_tokens']:.1f}×** "
            f"| {s['wall_seconds']}s | {m['wall_seconds']}s "
            f"| {m['wall_seconds'] / s['wall_seconds']:.1f}× |"
        )
    return "\n".join(lines)


def table_attribution(records: list[dict]) -> str:
    lines = [
        "| 任务 | Planner | Executor | Evaluator | 合计 |",
        "|------|---------|----------|-----------|------|",
    ]
    for r in records:
        if r["mode"] != "multi":
            continue
        by = r["by_agent"]

        def cell(name: str, by=by, total=r["total_tokens"]) -> str:
            a = by.get(name)
            if not a:
                return "—"
            return f"{a['total_tokens']} ({a['total_tokens'] / total * 100:.0f}%)"

        lines.append(
            f"| {r['task']} | {cell('planner')} | {cell('executor')} "
            f"| {cell('evaluator')} | {r['total_tokens']} |"
        )
    return "\n".join(lines)


def table_tool_calls(records: list[dict]) -> str:
    lines = [
        "| 任务 | 单 Agent 工具调用 | 多 Agent 工具调用 | 单 Agent 调用轮次 | 多 Agent 调用轮次 |",
        "|------|-----------------|-----------------|-----------------|-----------------|",
    ]
    by_task: dict[str, dict[str, dict]] = {}
    for r in records:
        by_task.setdefault(r["task"], {})[r["mode"]] = r
    for task, modes in by_task.items():
        if "solo" not in modes or "multi" not in modes:
            continue
        s, m = modes["solo"], modes["multi"]
        lines.append(
            f"| {task} | {count_tools(s)} | {count_tools(m)} "
            f"| {s['llm_calls']} | {m['llm_calls']} |"
        )
    return "\n".join(lines)


def count_tools(record: dict) -> int:
    log = json.loads(Path(record["log_file"]).read_text(encoding="utf-8"))
    return sum(1 for e in log["events"] if e["type"] == "tool_call")


def main() -> None:
    data = load(sys.argv[1] if len(sys.argv) > 1 else None)
    records = data["records"]
    budget = data.get("budget", {})

    print(f"预算: `{json.dumps(budget, ensure_ascii=False)}`\n")
    print("### 总览\n")
    print(table_main(records))
    print("\n### 多 Agent 相对单 Agent 的开销\n")
    print(table_multiplier(records))
    print("\n### 多 Agent 的 token 归因\n")
    print(table_attribution(records))
    print("\n### 工具调用次数\n")
    print(table_tool_calls(records))


if __name__ == "__main__":
    main()
