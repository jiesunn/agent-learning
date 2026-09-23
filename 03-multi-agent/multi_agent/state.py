# coding: utf-8
"""03 章的核心：把"多 Agent 协作"写成一个**显式状态机**。

为什么不交给 LLM 自己决定下一步
--------------------------------
01/02 的单 Agent 循环里，模型通过 ``finish_reason`` 决定"继续调工具"还是"结束"，
这个自由度是安全的：最坏情况是多调一次工具。

到了多角色场景，如果让 Planner 在 prompt 里说"现在该 Executor 上了"，
你会同时失去三样东西：

- **不可测**：没有 LLM 就跑不了任何流程，状态机无法单测
- **不可控**：模型可以在"评估没通过"时自己在文本里宣布通过
- **不可观测**：出问题时分不清是模型判断错，还是编排逻辑错

所以这一章的分工是：

    Agent 只产出「建议」（结构化数据），状态机负责「裁决」（纯函数）。

``decide()`` 是纯函数：输入 ``RunState`` + ``Budget``，输出 ``Decision``。
不碰网络、不碰 LLM，因此可以 100% 被 ``tests/test_state.py`` 覆盖。

三件容易被混淆的事
------------------
1. ``Verdict`` 是 Evaluator 的**建议**（pass/revise/replan）
2. ``Budget`` 是**硬约束**，优先级高于任何建议
3. ``StepStatus`` 是**裁决结果**，只有 PASSED 才会被跳过

Evaluator 说 revise，但重试次数已用尽 —— 此时不是"听模型的"，而是状态机升级为
重规划；重规划预算也没了，就判失败。模型永远不能覆盖预算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ----------------------------------------------------------------
# 枚举
# ----------------------------------------------------------------


class Action(StrEnum):
    """状态机的 5 个动作。全集就这些，没有隐式分支。"""

    PLAN = "plan"
    EXECUTE = "execute"
    EVALUATE = "evaluate"
    FINISH = "finish"
    ABORT = "abort"


class Verdict(StrEnum):
    """Evaluator 的三种建议。"""

    PASS = "pass"      # 本步达成
    REVISE = "revise"  # 方向对、产物有缺陷，原计划内重做本步
    REPLAN = "replan"  # 计划本身有问题，必须改计划


class StepStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"


# ----------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------


@dataclass
class Step:
    """计划中的一步。

    生命周期::

        attempts=0, verdict=None            # 还没执行
        attempts=1, verdict=None            # 执行完，等评估 —— decide() 会返回 EVALUATE
        attempts=1, verdict=REVISE          # 评估建议重做 —— decide() 会返回 EXECUTE
        attempts=1, verdict=PASS            # status=PASSED，永远不会再被选中
    """

    id: int
    description: str
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    attempts: int = 0
    verdict: Verdict | None = None
    last_feedback: str = ""

    @property
    def done(self) -> bool:
        return self.status is StepStatus.PASSED


@dataclass
class Plan:
    """一份计划。revision 每次重规划 +1。"""

    goal: str
    steps: list[Step] = field(default_factory=list)
    revision: int = 0
    note: str = ""


@dataclass
class Budget:
    """硬约束。所有数字都是"上限"，触顶即 ABORT。

    ``max_attempts_per_step=2`` 表示：首次执行 + 1 次 revise 重试。
    """

    max_steps: int = 6
    max_attempts_per_step: int = 2
    max_replans: int = 1
    max_llm_calls: int = 40
    max_total_tokens: int = 300_000


@dataclass
class RunState:
    """一次运行的全部可观测状态。

    刻意不含任何 LLM 对象 —— 所以 ``decide()`` 天然可测。
    """

    goal: str
    plan: Plan | None = None
    replans: int = 0
    llm_calls: int = 0
    total_tokens: int = 0
    status: str = "running"  # running | success | failed
    abort_reason: str = ""
    final_answer: str = ""

    def current_step(self) -> Step | None:
        """第一个尚未通过的步骤。计划是有序的，所以按 index 找。"""
        if self.plan is None:
            return None
        return next((s for s in self.plan.steps if not s.done), None)

    def completed_steps(self) -> list[Step]:
        if self.plan is None:
            return []
        return [s for s in self.plan.steps if s.done]

    def sync_usage(self, llm_calls: int, total_tokens: int) -> None:
        """把 logger 的真实用量同步进来，供 decide() 判断预算。

        状态机不自己数 token —— 数 token 是 LLM 客户端的事，两边各数一份必然不一致。
        """
        self.llm_calls = llm_calls
        self.total_tokens = total_tokens


@dataclass
class Decision:
    """一次状态转移。reason 会被写进日志 —— 出问题时先看这些字符串。"""

    action: Action
    step: Step | None = None
    reason: str = ""


# ----------------------------------------------------------------
# 状态机（纯函数）
# ----------------------------------------------------------------


def decide(state: RunState, budget: Budget) -> Decision:
    """决定下一步做什么。这是整个 03 章唯一的控制流入口。

    判定顺序是有意为之的：预算 -> 无计划 -> 全部完成 -> 重规划 -> 待评估 -> 重试 -> 执行。
    """
    # 1. 预算优先于一切。检查在"下一次调用之前"，不是"这一轮之后"。
    if state.llm_calls >= budget.max_llm_calls:
        return Decision(
            Action.ABORT,
            reason=f"llm_calls_exhausted({state.llm_calls}/{budget.max_llm_calls})",
        )
    if state.total_tokens >= budget.max_total_tokens:
        return Decision(
            Action.ABORT,
            reason=f"tokens_exhausted({state.total_tokens}/{budget.max_total_tokens})",
        )

    # 2. 没有计划 -> 先规划
    if state.plan is None:
        return Decision(Action.PLAN, reason="no_plan")

    step = state.current_step()

    # 3. 所有步骤已通过 -> 结束
    if step is None:
        return Decision(Action.FINISH, reason="all_steps_passed")

    # 4. Evaluator 明确要求重规划
    if step.verdict is Verdict.REPLAN:
        if state.replans >= budget.max_replans:
            return Decision(
                Action.ABORT,
                step=step,
                reason=f"replan_exhausted(step={step.id})",
            )
        return Decision(Action.PLAN, step=step, reason=f"evaluator_requested_replan(step={step.id})")

    # 5. 有产物但还没评估 -> 去评估
    if step.attempts > 0 and step.verdict is None:
        return Decision(Action.EVALUATE, step=step, reason=f"step_{step.id}_awaiting_verdict")

    # 6. 重试次数用尽仍未通过 -> 不再重试，升级为重规划（模型说了不算，预算说了算）
    if step.attempts >= budget.max_attempts_per_step:
        if state.replans >= budget.max_replans:
            return Decision(
                Action.ABORT,
                step=step,
                reason=f"attempts_exhausted(step={step.id}, attempts={step.attempts})",
            )
        return Decision(
            Action.PLAN,
            step=step,
            reason=f"escalate_to_replan(step={step.id}, attempts={step.attempts})",
        )

    # 7. 默认：执行下一步
    return Decision(Action.EXECUTE, step=step, reason=f"step_{step.id}_attempt_{step.attempts + 1}")
