# coding: utf-8
"""状态机单测。

这个文件是 03 章"显式状态机"这个设计选择的**唯一证据**：
一个多 Agent 系统如果编排逻辑写在 prompt 里，这些用例一个都写不出来。

每个用例只构造 RunState 然后断言 decide() 的返回，不发一次网络请求。
"""
from __future__ import annotations

import pytest

from multi_agent.state import (
    Action,
    Budget,
    Plan,
    RunState,
    Step,
    StepStatus,
    Verdict,
    decide,
)


def make_state(*steps: Step, plan_revision: int = 0, **kwargs) -> RunState:
    state = RunState(goal="测试目标", **kwargs)
    state.plan = Plan(goal=state.goal, steps=list(steps), revision=plan_revision)
    return state


def step(id_: int, status: StepStatus = StepStatus.PENDING, **kwargs) -> Step:
    return Step(id=id_, description=f"步骤 {id_}", status=status, **kwargs)


# ----------------------------------------------------------------
# 1. 规划阶段
# ----------------------------------------------------------------
def test_no_plan_returns_plan():
    decision = decide(RunState(goal="g"), Budget())
    assert decision.action is Action.PLAN
    assert decision.reason == "no_plan"


# ----------------------------------------------------------------
# 2. 正常推进
# ----------------------------------------------------------------
def test_fresh_step_returns_execute():
    decision = decide(make_state(step(1)), Budget())
    assert decision.action is Action.EXECUTE
    assert decision.step.id == 1
    assert decision.reason == "step_1_attempt_1"


def test_step_with_result_awaits_verdict_then_evaluates():
    """执行完但还没评估 —— 必须先评估，不能直接算通过。"""
    decision = decide(make_state(step(1, attempts=1, result="done")), Budget())
    assert decision.action is Action.EVALUATE


def test_all_steps_passed_returns_finish():
    decision = decide(make_state(step(1, StepStatus.PASSED), step(2, StepStatus.PASSED)), Budget())
    assert decision.action is Action.FINISH
    assert decision.reason == "all_steps_passed"


def test_current_step_skips_passed_steps_in_order():
    state = make_state(step(1, StepStatus.PASSED), step(2), step(3))
    decision = decide(state, Budget())
    assert decision.action is Action.EXECUTE
    assert decision.step.id == 2


def test_revise_verdict_retries_same_step():
    state = make_state(step(1, attempts=1, verdict=Verdict.REVISE, last_feedback="数字算错了"))
    decision = decide(state, Budget(max_attempts_per_step=2))
    assert decision.action is Action.EXECUTE
    assert decision.step.id == 1
    assert decision.reason == "step_1_attempt_2"


# ----------------------------------------------------------------
# 3. 重试 / 重规划 / 失败 的三岔口
# ----------------------------------------------------------------
def test_attempts_exhausted_escalates_to_replan():
    """Evaluator 说 revise，但重试次数用尽 —— 状态机升级为重规划，而不是听模型的。"""
    state = make_state(step(1, attempts=2, verdict=Verdict.REVISE))
    decision = decide(state, Budget(max_attempts_per_step=2, max_replans=1))
    assert decision.action is Action.PLAN
    assert decision.reason.startswith("escalate_to_replan")


def test_attempts_exhausted_and_no_replan_budget_aborts():
    state = make_state(step(1, attempts=2, verdict=Verdict.REVISE))
    decision = decide(state, Budget(max_attempts_per_step=2, max_replans=0))
    assert decision.action is Action.ABORT
    assert decision.reason.startswith("attempts_exhausted")


def test_evaluator_replan_request_triggers_replan():
    state = make_state(step(1, attempts=1, verdict=Verdict.REPLAN))
    decision = decide(state, Budget(max_replans=1))
    assert decision.action is Action.PLAN
    assert decision.reason.startswith("evaluator_requested_replan")


def test_replan_budget_exhausted_aborts():
    state = make_state(step(1, attempts=1, verdict=Verdict.REPLAN), replans=1)
    decision = decide(state, Budget(max_replans=1))
    assert decision.action is Action.ABORT
    assert decision.reason.startswith("replan_exhausted")


def test_replan_budget_is_global_not_per_step():
    """max_replans=1 表示整条流程只能重规划一次，不是每个步骤一次。"""
    state = make_state(step(1), step(2), replans=1)
    decisions = set()
    # 步骤 1 用掉重规划额度后，步骤 2 再想重规划就走不通了
    state.plan.steps[0].verdict = Verdict.REPLAN
    state.plan.steps[0].attempts = 1
    decisions.add(decide(state, Budget(max_replans=1)).action)

    state.plan.steps[0] = step(1, StepStatus.PASSED)
    state.plan.steps[1].verdict = Verdict.REPLAN
    state.plan.steps[1].attempts = 1
    decisions.add(decide(state, Budget(max_replans=1)).action)

    assert decisions == {Action.ABORT}


# ----------------------------------------------------------------
# 4. 预算优先级：高于一切，包括"看起来已经做完了"
# ----------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs, reason_part",
    [
        ({"llm_calls": 10}, "llm_calls_exhausted"),
        ({"total_tokens": 1000}, "tokens_exhausted"),
    ],
)
def test_budget_aborts_before_anything_else(kwargs, reason_part):
    state = make_state(step(1), **kwargs)
    decision = decide(state, Budget(max_llm_calls=10, max_total_tokens=1000))
    assert decision.action is Action.ABORT
    assert decision.reason.startswith(reason_part)


def test_budget_beats_finish():
    """所有步骤都通过了，但预算已爆 —— 仍然是 ABORT。

    这条容易被写成"都做完了就放行"，那样预算约束在最后一步失效。
    """
    state = make_state(step(1, StepStatus.PASSED), llm_calls=99)
    decision = decide(state, Budget(max_llm_calls=10))
    assert decision.action is Action.ABORT


def test_budget_beats_plan():
    decision = decide(RunState(goal="g", total_tokens=10_000), Budget(max_total_tokens=10_000))
    assert decision.action is Action.ABORT


def test_default_budget_is_not_exceeded_by_fresh_state():
    decision = decide(RunState(goal="g"), Budget())
    assert decision.action is Action.PLAN
