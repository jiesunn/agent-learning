# coding: utf-8
"""两个可比的入口：多 Agent 与单 Agent 基线。

为什么基线必须和被测系统放在一个模块里
--------------------------------------
"多 Agent 比单 Agent 好/差"这个结论，只有在**模型相同、工具相同、任务相同、
预算口径相同**时才成立。如果基线写在另一个脚本里各写各的日志格式，
对比就变成了"比两份代码"，而不是"比两种架构"。

所以这里两个函数共享：

- 同一个 ``Agent`` 基类（同一套工具循环、同一套 JSON 处理）
- 同一个 ``RunLogger``（token 归因口径一致）
- 同一个 ``Budget``（预算上限一致）

唯一不同的是**控制流**：一个是 ``decide()`` 驱动的状态机，一个是一条道走到黑。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agents import Agent, BudgetExceeded
from .llm import LLMClient, get_current_client
from .logger import RunLogger
from .orchestrator import Orchestrator
from .state import Budget, RunState

ALL_TOOLS = {"read_file", "write_file", "list_files"}

SOLO_SYSTEM = """你是一个助手，可以使用 read_file / write_file / list_files 三个工具完成任务。

规则：
1. 需要读写文件时调用工具，不要凭空猜测文件内容。
2. 用户的全部要求都要完成，不要只做一部分就收尾。
3. 全部完成后，用不超过 5 句话总结：做了什么、产物路径、关键结果。"""

SOLO_MAX_ROUNDS = 12


@dataclass
class RunResult:
    mode: str
    status: str
    final_answer: str
    log_file: Path
    logger: RunLogger
    state: RunState | None = None

    def summary(self) -> dict:
        return {"mode": self.mode, "status": self.status, **self.logger.summary()}


def _guard_for(logger: RunLogger, budget: Budget):
    def guard() -> None:
        if logger.llm_calls >= budget.max_llm_calls:
            raise BudgetExceeded(f"llm_calls {logger.llm_calls} >= {budget.max_llm_calls}")
        if logger.total_tokens >= budget.max_total_tokens:
            raise BudgetExceeded(f"tokens {logger.total_tokens} >= {budget.max_total_tokens}")

    return guard


def run_multi(
    goal: str,
    *,
    budget: Budget | None = None,
    verbose: bool = True,
    llm: LLMClient | None = None,
) -> RunResult:
    """Planner / Executor / Evaluator + 显式状态机。"""
    llm = llm or get_current_client()
    logger = RunLogger(goal, model=llm.model, mode="multi")
    orchestrator = Orchestrator(llm=llm, logger=logger, budget=budget)

    state = orchestrator.run(goal, verbose=verbose)
    log_file = logger.finish(
        status=state.status,
        final_answer=state.final_answer,
        state=state,
    )
    return RunResult("multi", state.status, state.final_answer, log_file, logger, state)


def run_solo(
    goal: str,
    *,
    budget: Budget | None = None,
    verbose: bool = True,
    llm: LLMClient | None = None,
    max_rounds: int = SOLO_MAX_ROUNDS,
) -> RunResult:
    """单 Agent 基线：同样工具、同样模型、同样预算，但没有角色拆分。"""
    llm = llm or get_current_client()
    budget = budget or Budget()
    logger = RunLogger(goal, model=llm.model, mode="solo")

    agent = Agent(llm, logger, _guard_for(logger, budget))
    agent.name = "solo"
    agent.system = SOLO_SYSTEM
    agent.tool_names = ALL_TOOLS

    logger.log_event("solo_start", max_rounds=max_rounds)
    status = "success"
    answer = ""
    try:
        loop = agent.run_tools(goal, label="solo", max_rounds=max_rounds)
        answer = loop.content
        if not loop.stopped:
            # 撞到轮数上限 = 没做完，不能算成功
            status = "max_rounds"
    except BudgetExceeded as e:
        logger.log_event("budget_exceeded", detail=str(e))
        status = "failed"

    logger.log_event("solo_end", status=status)
    log_file = logger.finish(status=status, final_answer=answer)
    return RunResult("solo", status, answer, log_file, logger)
