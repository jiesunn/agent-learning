# coding: utf-8
"""状态机驱动 + 三个 Agent 的装配。

编排层只做四件事：
  1. 调 ``decide()`` 拿到下一步动作
  2. 调用对应 Agent 执行这个动作
  3. 把结果写回 ``RunState``
  4. 记账（token / 调用次数）供下一轮 ``decide()`` 判断预算

它**不含任何智能**。所有"应该怎么做"的判断都在 Agent 里，
所有"允不允许"的判断都在 ``decide()`` 里。这是 03 章能讲清楚的前提。
"""
from __future__ import annotations

from .agents import (
    BudgetExceeded,
    Evaluator,
    Executor,
    Planner,
    truncate,
)
from .llm import LLMClient
from .logger import RunLogger
from .state import Action, Budget, Decision, RunState, Step, StepStatus, Verdict, decide


def format_plan(state: RunState) -> str:
    if state.plan is None:
        return "(无计划)"
    lines = [f"计划 v{state.plan.revision + 1}（{len(state.plan.steps)} 步）:"]
    for s in state.plan.steps:
        mark = "✅" if s.done else "⬜"
        lines.append(f"  {mark} {s.id}. {s.description}")
    return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        *,
        llm: LLMClient,
        logger: RunLogger,
        budget: Budget | None = None,
        max_iterations: int = 60,
    ):
        self.llm = llm
        self.logger = logger
        self.budget = budget or Budget()
        # 状态机自己也要有保险丝：万一 decide() 有 bug 导致动作不改变状态，
        # 这里会在有限步内退出，而不是挂死。**永不相信"循环一定会结束"。**
        self.max_iterations = max_iterations

        guard = self._check_budget
        self.planner = Planner(llm, logger, guard)
        self.executor = Executor(llm, logger, guard)
        self.evaluator = Evaluator(llm, logger, guard)

    # ------------------------------------------------------------
    # 预算守卫：在每次 LLM 调用前触发
    # ------------------------------------------------------------
    def _check_budget(self) -> None:
        if self.logger.llm_calls >= self.budget.max_llm_calls:
            raise BudgetExceeded(
                f"llm_calls {self.logger.llm_calls} >= {self.budget.max_llm_calls}"
            )
        if self.logger.total_tokens >= self.budget.max_total_tokens:
            raise BudgetExceeded(
                f"tokens {self.logger.total_tokens} >= {self.budget.max_total_tokens}"
            )

    # ------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------
    def run(self, goal: str, *, verbose: bool = True) -> RunState:
        state = RunState(goal=goal)
        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1
            state.sync_usage(self.logger.llm_calls, self.logger.total_tokens)
            decision = decide(state, self.budget)

            self.logger.log_event(
                "decision",
                iteration=iteration,
                action=str(decision.action),
                reason=decision.reason,
                step=decision.step.id if decision.step else None,
                llm_calls=state.llm_calls,
                total_tokens=state.total_tokens,
            )
            if verbose:
                print(f"\n{'─' * 62}")
                print(f"▸ [{iteration}] {decision.action.upper()}  ({decision.reason})")

            if decision.action is Action.FINISH:
                state.status = "success"
                state.final_answer = self._synthesize(state)
                self.logger.log_event("finished", reason=decision.reason)
                break

            if decision.action is Action.ABORT:
                self._abort(state, decision.reason)
                break

            try:
                self._dispatch(state, decision, verbose=verbose)
            except BudgetExceeded as e:
                self.logger.log_event("budget_exceeded", detail=str(e))
                self._abort(state, f"budget_exceeded: {e}")
                break

            if state.status != "running":
                break
        else:
            self._abort(state, f"iteration_fuse({self.max_iterations})")

        state.sync_usage(self.logger.llm_calls, self.logger.total_tokens)
        return state

    # ------------------------------------------------------------
    # 动作分发
    # ------------------------------------------------------------
    def _dispatch(self, state: RunState, decision: Decision, *, verbose: bool) -> None:
        if decision.action is Action.PLAN:
            self._do_plan(state, decision, verbose=verbose)
        elif decision.action is Action.EXECUTE:
            self._do_execute(state, decision.step, verbose=verbose)
        elif decision.action is Action.EVALUATE:
            self._do_evaluate(state, decision.step, verbose=verbose)
        else:  # pragma: no cover - decide() 只返回上面几种，留作断言
            self._abort(state, f"unhandled_action({decision.action})")

    def _do_plan(self, state: RunState, decision: Decision, *, verbose: bool) -> None:
        if state.plan is None:
            plan = self.planner.plan(state.goal, self.budget.max_steps)
            if plan is None:
                self._abort(state, "planner_no_valid_plan")
                return
            state.plan = plan
            self.logger.log_event(
                "plan_created",
                revision=plan.revision,
                steps=[{"id": s.id, "description": s.description} for s in plan.steps],
            )
            if verbose:
                print(format_plan(state))
            return

        plan = self.planner.replan(
            goal=state.goal,
            plan=state.plan,
            failing_step=decision.step,
            max_steps=self.budget.max_steps,
        )
        if plan is None:
            self._abort(state, "replanner_no_valid_plan")
            return

        state.replans += 1
        state.plan = plan
        self.logger.log_event(
            "plan_replanned",
            revision=plan.revision,
            trigger_reason=decision.reason,
            note=plan.note,
            steps=[{"id": s.id, "description": s.description} for s in plan.steps],
        )
        if verbose:
            print(format_plan(state))

    def _do_execute(self, state: RunState, step: Step, *, verbose: bool) -> None:
        assert state.plan is not None
        step.attempts += 1
        self.logger.log_event(
            "step_execute", step=step.id, attempt=step.attempts, description=step.description
        )
        if verbose:
            print(f"▶️  执行步骤 {step.id}（第 {step.attempts} 次）：{step.description}")

        loop = self.executor.execute(
            goal=state.goal,
            plan=state.plan,
            step=step,
            label=f"exec_step{step.id}_try{step.attempts}",
        )

        step.result = loop.content or "(Executor 工具循环达到上限，未给出文字汇报)"
        # 产物变了，上一次的评估立即作废 —— 否则会出现"用旧评估放行新产物"
        step.verdict = None

        self.logger.log_event(
            "step_executed",
            step=step.id,
            attempt=step.attempts,
            rounds=loop.rounds,
            stopped=loop.stopped,
            tool_calls=len(loop.tool_calls),
            result=truncate(step.result, 800),
        )
        if verbose:
            status = "正常收尾" if loop.stopped else "⚠️ 撞到工具循环上限"
            print(f"    {loop.rounds} 轮 / {len(loop.tool_calls)} 次工具调用（{status}）")
            print(f"    汇报：{truncate(step.result, 200)}")

    def _do_evaluate(self, state: RunState, step: Step, *, verbose: bool) -> None:
        assert state.plan is not None
        if verbose:
            print(f"🔍 评估步骤 {step.id}（第 {step.attempts} 次产出）...")

        evaluation = self.evaluator.evaluate(goal=state.goal, plan=state.plan, step=step)

        step.verdict = evaluation.verdict
        step.last_feedback = evaluation.feedback or evaluation.reason
        if evaluation.verdict is Verdict.PASS:
            step.status = StepStatus.PASSED

        self.logger.log_event(
            "step_evaluated",
            step=step.id,
            attempt=step.attempts,
            verdict=str(evaluation.verdict),
            reason=evaluation.reason,
            feedback=evaluation.feedback,
        )
        if verbose:
            icon = {"pass": "✅", "revise": "🔁", "replan": "🧭"}[str(evaluation.verdict)]
            print(f"    {icon} {evaluation.verdict.upper()} | {evaluation.reason}")
            if evaluation.verdict is not Verdict.PASS and evaluation.feedback:
                print(f"    反馈：{truncate(evaluation.feedback, 200)}")

    # ------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------
    @staticmethod
    def _synthesize(state: RunState) -> str:
        """把各步骤的产出拼成交付说明。

        刻意**不**再调一次 LLM 做总结：那会引入一次"总结可能失真"的风险，
        而步骤产出的原文已经在日志里了。要总结让用户自己去问。
        """
        assert state.plan is not None
        parts = [
            f"[步骤 {s.id}] {s.description}\n{s.result}"
            for s in state.plan.steps
            if s.done
        ]
        return "\n\n".join(parts)

    def _abort(self, state: RunState, reason: str) -> None:
        state.status = "failed"
        state.abort_reason = reason
        self.logger.log_event("aborted", reason=reason)
        print(f"\n❌ 流程终止：{reason}")
