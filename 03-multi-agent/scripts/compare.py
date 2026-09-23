# coding: utf-8
"""三任务 × 两模式的对照实验，带**独立的产物验收**。

为什么要有独立验收
------------------
状态机报 success 只代表"Evaluator 说 pass"。这是**模型自评**，不是事实。
所以每个任务额外写一个 `verify_*` 函数，直接读 workspace 里的产物做程序化断言：

    状态机说 success，产物验收不通过  ->  自评失效，这是最值得记录的一种结果
    状态机说 failed， 产物验收通过    ->  过度保守，也是成本

只有把这两个口径都记下来，"多 Agent 值不值"才是一个可以被证伪的问题。

跑法（必须从 03-multi-agent 目录执行，tools.py 的 WORKSPACE 相对 cwd）::

    uv run python scripts/compare.py
    uv run python scripts/compare.py --only sales
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from multi_agent.runner import run_multi, run_solo  # noqa: E402
from multi_agent.state import Budget  # noqa: E402

FIXTURES = ROOT / "fixtures"
WORKSPACE = ROOT / "workspace"
LOG_DIR = ROOT / "logs"


# ----------------------------------------------------------------
# 验收函数：只读产物，不看 Agent 说了什么
# ----------------------------------------------------------------
def verify_hello(ws: Path) -> tuple[bool, list[str]]:
    problems: list[str] = []
    target = ws / "hello.txt"
    if not target.exists():
        problems.append("hello.txt 不存在")
    elif "Hello Agent" not in target.read_text(encoding="utf-8"):
        problems.append("hello.txt 内容不含 'Hello Agent'")
    return not problems, problems


def verify_sales(ws: Path) -> tuple[bool, list[str]]:
    """报告里的 4 个金额必须都对（容差 0.05）。

    Widget 24×19.9=477.6 / Gadget 9×45.5=409.5 / Doohickey 20×2.5=50.0 / 合计 937.1
    """
    problems: list[str] = []
    target = ws / "report.md"
    if not target.exists():
        return False, ["report.md 不存在"]

    text = target.read_text(encoding="utf-8")
    numbers = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    for expected, label in [
        (477.6, "Widget 销售额"),
        (409.5, "Gadget 销售额"),
        (50.0, "Doohickey 销售额"),
        (937.1, "总销售额"),
    ]:
        if not any(abs(n - expected) < 0.05 for n in numbers):
            problems.append(f"{label} 期望 ≈{expected}，报告里找不到")
    return not problems, problems


def verify_requirements(ws: Path) -> tuple[bool, list[str]]:
    """正确的答案是"以日期最新的来源为准" = email.txt(2026-02-14)。

    project=Atlas / deadline=2026-08-15 / budget=60万 / owner=张伟
    """
    problems: list[str] = []

    target = ws / "summary.json"
    if not target.exists():
        problems.append("summary.json 不存在")
    else:
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
            problems.append("summary.json 不是合法 JSON")
        if isinstance(data, dict):
            for key, needle in [
                ("project", "atlas"),
                ("deadline", "2026-08-15"),
                ("budget", "60"),
                ("owner", "张伟"),
            ]:
                got = str(data.get(key, "")).strip().lower()
                if needle.lower() not in got:
                    problems.append(f"summary.json 的 {key} 期望含 {needle!r}，实际 {got!r}")
        elif data is not None:
            problems.append("summary.json 顶层不是对象")

    conflicts = ws / "conflicts.md"
    if not conflicts.exists():
        problems.append("conflicts.md 不存在")
    else:
        text = conflicts.read_text(encoding="utf-8")
        for name in ("interview.txt", "email.txt", "spec.txt"):
            if name not in text:
                problems.append(f"conflicts.md 未提到来源 {name}")

    return not problems, problems


@dataclass
class Task:
    key: str
    fixture: str | None
    goal: str
    verify: Callable[[Path], tuple[bool, list[str]]]


TASKS: list[Task] = [
    Task(
        key="hello",
        fixture=None,  # 空 workspace
        goal=(
            "在 workspace 下创建 hello.txt，内容为 'Hello Agent'；"
            "如果文件已存在，就保留原内容并在末尾追加一行 '-- checked'。"
        ),
        verify=verify_hello,
    ),
    Task(
        key="sales",
        fixture="task_sales",
        goal=(
            "读取 workspace/raw/sales.csv，按产品汇总销售额（每行 units × unit_price，"
            "同产品求和），把结果写成 workspace/report.md：一个 Markdown 表格，"
            "列为「产品 | 销量 | 销售额」，表格之后单独一行给出总销售额。"
            "金额保留一位小数。"
        ),
        verify=verify_sales,
    ),
    Task(
        key="requirements",
        fixture="task_requirements",
        goal=(
            "workspace/raw/ 下有 3 个来源文件（spec.txt / interview.txt / email.txt），"
            "它们对同一个项目的需求描述互相冲突。请交叉比对，产出两个文件：\n"
            "1) workspace/summary.json，字段为 project / deadline / budget / owner；\n"
            "2) workspace/conflicts.md，逐条列出来源之间不一致的地方，"
            "说明你采信哪一个、依据是什么。\n"
            "裁决规则：以文件内标注日期最新的来源为准。"
        ),
        verify=verify_requirements,
    ),
]


# ----------------------------------------------------------------
# 执行
# ----------------------------------------------------------------
def reset_workspace(fixture: str | None) -> None:
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    if fixture is None:
        WORKSPACE.mkdir(parents=True)
    else:
        shutil.copytree(FIXTURES / fixture, WORKSPACE)


def snapshot(ws: Path) -> dict:
    """把产物的内容抓下来存进对比结果 —— 事后复盘不用再去翻 workspace。"""
    out = {}
    for p in sorted(ws.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(ws))
            try:
                out[rel] = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                out[rel] = "<binary>"
    return out


def run_once(task: Task, mode: str, budget: Budget, *, verbose: bool) -> dict:
    reset_workspace(task.fixture)
    print(f"\n{'#' * 62}")
    print(f"# 任务={task.key}  模式={mode}")
    print(f"{'#' * 62}")

    started = time.time()
    if mode == "multi":
        result = run_multi(task.goal, budget=budget, verbose=verbose)
    else:
        result = run_solo(task.goal, budget=budget, verbose=verbose)
    wall = time.time() - started

    ok, problems = task.verify(WORKSPACE)
    artifacts = snapshot(WORKSPACE)
    summary = result.logger.summary()
    state = result.state

    record = {
        "task": task.key,
        "mode": mode,
        "model": result.logger.model,
        "agent_status": result.status,
        "artifact_ok": ok,
        "artifact_problems": problems,
        "agreement": (result.status == "success") == ok,
        "wall_seconds": round(wall, 2),
        "llm_calls": summary["llm_calls"],
        "total_tokens": summary["total_tokens"],
        "prompt_tokens": summary["total_prompt_tokens"],
        "completion_tokens": summary["total_completion_tokens"],
        "llm_duration_ms": summary["llm_duration_ms"],
        "retries": summary["retries"],
        "by_agent": summary["by_agent"],
        "num_steps": len(state.plan.steps) if state and state.plan else None,
        "replans": state.replans if state else None,
        "abort_reason": state.abort_reason if state else "",
        "final_answer": (result.final_answer or "")[:2000],
        "artifacts": artifacts,
        "log_file": str(result.log_file),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    _print_record(record)
    return record


def _print_record(r: dict) -> None:
    print(f"\n{'=' * 62}")
    print(f"结果  任务={r['task']} 模式={r['mode']}")
    print(f"  状态机判定 : {r['agent_status']}")
    print(f"  产物验收   : {'PASS' if r['artifact_ok'] else 'FAIL'}  {r['artifact_problems']}")
    print(f"  两者一致   : {r['agreement']}")
    print(f"  调用/耗时  : {r['llm_calls']} 次 / {r['wall_seconds']}s")
    print(f"  tokens     : {r['total_tokens']} (prompt {r['prompt_tokens']})")
    print(f"  产物       : {sorted(r['artifacts'])}")
    print("=" * 62)


def main() -> None:
    parser = argparse.ArgumentParser(description="multi-agent vs solo 对照实验")
    parser.add_argument("--only", default=None, help="只跑某个任务，逗号分隔")
    parser.add_argument("--mode", choices=["multi", "solo", "both"], default="both")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=5)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-replans", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--token-budget", type=int, default=200_000)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    budget = Budget(
        max_steps=args.max_steps,
        max_attempts_per_step=args.max_attempts,
        max_replans=args.max_replans,
        max_llm_calls=args.max_calls,
        max_total_tokens=args.token_budget,
    )

    selected = TASKS
    if args.only:
        wanted = {k.strip() for k in args.only.split(",")}
        selected = [t for t in TASKS if t.key in wanted]
        if not selected:
            parser.error(f"没有匹配的任务: {wanted}")

    modes = ["multi", "solo"] if args.mode == "both" else [args.mode]

    records = []
    for repeat in range(args.repeat):
        for task in selected:
            for mode in modes:
                if args.repeat > 1:
                    print(f"\n>>> repeat {repeat + 1}/{args.repeat}")
                records.append(run_once(task, mode, budget, verbose=not args.quiet))

    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = LOG_DIR / f"compare_{stamp}.json"
    out_file.write_text(
        json.dumps(
            {"budget": vars(budget), "records": records},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n\n{'=' * 96}")
    print("汇总  (✅=状态机 success / 产物验收通过；⚠️=两者不一致)")
    print("=" * 96)
    header = (
        f"{'task':<14}{'mode':<8}{'状态':<9}{'验收':<7}{'一致':<7}"
        f"{'调用':>5}{'tokens':>9}{'耗时s':>8}{'步骤':>5}{'重规划':>7}"
    )
    print(header)
    print("-" * 96)
    for r in records:
        print(
            f"{r['task']:<14}{r['mode']:<8}{r['agent_status']:<9}"
            f"{'PASS' if r['artifact_ok'] else 'FAIL':<7}"
            f"{'✅' if r['agreement'] else '⚠️':<7}"
            f"{r['llm_calls']:>5}{r['total_tokens']:>9}{r['wall_seconds']:>8}"
            f"{str(r['num_steps']):>5}{str(r['replans']):>7}"
        )
    print("=" * 96)
    print(f"\n原始数据: {out_file}")


if __name__ == "__main__":
    main()
