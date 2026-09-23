# coding: utf-8
"""三个角色：Planner / Executor / Evaluator。

角色分工
--------
============  ==================  ==========================
角色           工具集              只做一件事
============  ==================  ==========================
Planner       无                  把目标拆成可验收的步骤
Executor      read/write/list     执行**一个**步骤，产出文件
Evaluator     read/list           独立核验这一步是否真的达成
============  ==================  ==========================

两条设计原则
------------
**1. 工具集是权限边界，不是提示词。**

Evaluator 只挂 ``read_file`` / ``list_files``。它不是"被要求不要改文件"，
而是**没有能力改**。否则评估器一旦觉得产物不合格，最省事的行为就是自己动手改，
"评估"退化成"自评自改"，多 Agent 的意义消失。
结构性约束（少给一个工具）比提示词约束（多写一句"请不要修改"）可靠得多。

**2. Executor 一次只执行一个步骤。**

它看不到"下一步要做什么"，只看到总目标 + 当前步骤 + 已完成步骤的产出摘要。
这不是缺陷，是多 Agent 的主要收益：**每次调用的上下文都是裁剪过的**。
一个 8 步任务如果让单 Agent 一口气做完，第 8 步的 prompt 里塞着前 7 步的全部过程；
拆成 8 次执行后，每次只需要"目标 + 计划 + 已完成摘要"。
代价是调用次数变多 —— 这个权衡在 notes 里有实测数字。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMClient
from .logger import RunLogger
from .parsing import extract_json
from .state import Plan, Step, Verdict
from .tools import execute_tool, get_tool_schemas, parse_tool_args

# 工具集 = 权限边界
EXECUTOR_TOOLS = {"read_file", "write_file", "list_files"}
EVALUATOR_TOOLS = {"read_file", "list_files"}
PLANNER_TOOLS: set[str] = set()

MAX_EXECUTOR_ROUNDS = 6
MAX_EVALUATOR_ROUNDS = 4

JSON_REPAIR_INSTRUCTION = (
    "你上一条回复不是合法 JSON，无法被程序解析。"
    "请只输出 JSON 对象本身：不要解释、不要 markdown 围栏、不要前后缀。"
)

# 预算守卫在**每一次 LLM 调用之前**触发，而不是"这一轮结束后再检查"。
# 否则一个 6 轮的 Executor 工具循环可以一次性把预算超掉。
Guard = Callable[[], None]


class BudgetExceeded(RuntimeError):
    """预算触顶。由 orchestrator 捕获，转成状态机的 ABORT。"""


# ----------------------------------------------------------------
# 返回值
# ----------------------------------------------------------------
@dataclass
class ChatResult:
    content: str
    finish_reason: str
    tool_calls: list[Any] = field(default_factory=list)


@dataclass
class ToolLoop:
    content: str
    messages: list[dict]
    rounds: int
    tool_calls: list[dict]
    stopped: bool  # True=模型自己收尾；False=撞到 max_rounds，产出不可信


@dataclass
class Evaluation:
    verdict: Verdict
    reason: str
    feedback: str


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} chars)"


# ----------------------------------------------------------------
# Agent 基类：负责"调 LLM + 记账 + 工具循环"这三件所有角色都要做的事
# ----------------------------------------------------------------
class Agent:
    name = "agent"
    system = ""
    tool_names: set[str] = set()

    def __init__(self, llm: LLMClient, logger: RunLogger, guard: Guard | None = None):
        self.llm = llm
        self.logger = logger
        self.guard = guard

    # ------------------------------------------------------------
    # 单次调用
    # ------------------------------------------------------------
    def chat(
        self,
        messages: list[dict],
        *,
        label: str,
        tools: list[dict] | None = None,
        temperature: float | None = 0.0,
    ) -> ChatResult:
        if self.guard is not None:
            self.guard()

        start = time.time()
        try:
            resp = self.llm.call(
                messages,
                tools=tools,
                temperature=temperature,
                on_retry=lambda n, e: self.logger.log_event(
                    "llm_retry",
                    agent=self.name,
                    label=label,
                    attempt=n,
                    error=f"{type(e).__name__}: {e}",
                ),
            )
        except Exception as e:
            self.logger.log_event(
                "llm_error", agent=self.name, label=label, error=f"{type(e).__name__}: {e}"
            )
            raise
        duration = time.time() - start

        # 归属 token 的唯一入口
        self.logger.log_llm_call(
            agent=self.name, label=label, messages=messages, response=resp, duration=duration
        )

        choice = resp.choices[0]
        msg = choice.message
        return ChatResult(
            content=msg.content or "",
            finish_reason=choice.finish_reason or "",
            tool_calls=list(msg.tool_calls or []),
        )

    # ------------------------------------------------------------
    # 工具循环（Executor 和 Evaluator 共用 —— 这是真的有两处调用点）
    # ------------------------------------------------------------
    def run_tools(self, user_text: str, *, label: str, max_rounds: int) -> ToolLoop:
        """跑到模型自己停下，或撞到 max_rounds。

        与 01/02 的差异：这里用 ``if not r.tool_calls`` 判断，而不是
        ``finish_reason == "tool_calls"``。有的服务在带 tool_calls 时仍返回
        finish_reason="stop"，按 finish_reason 判断会把工具调用漏掉。
        """
        messages: list[dict] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": user_text},
        ]
        tools = get_tool_schemas(self.tool_names)
        trace: list[dict] = []

        for i in range(max_rounds):
            r = self.chat(messages, label=f"{label}#{i + 1}", tools=tools)

            assistant: dict = {"role": "assistant", "content": r.content}
            if r.tool_calls:
                assistant["tool_calls"] = [tc.model_dump() for tc in r.tool_calls]
            messages.append(assistant)

            if not r.tool_calls:
                return ToolLoop(r.content, messages, i + 1, trace, stopped=True)

            for tc in r.tool_calls:
                name = tc.function.name
                args = parse_tool_args(tc.function.arguments)
                result = execute_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                trace.append({"tool": name, "args": args, "result": result})
                self.logger.log_event(
                    "tool_call",
                    agent=self.name,
                    round=i + 1,
                    tool=name,
                    args=args,
                    result=truncate(result, 2000),
                )

        self.logger.log_event(
            "tool_loop_truncated", agent=self.name, label=label, max_rounds=max_rounds
        )
        return ToolLoop("", messages, max_rounds, trace, stopped=False)

    # ------------------------------------------------------------
    # 要求 JSON 输出的调用 + 一次修复
    # ------------------------------------------------------------
    def request_json(self, user_text: str, *, label: str) -> dict | None:
        """单轮 JSON 调用（Planner 用）。失败时最多再花一次调用做格式修复。"""
        messages: list[dict] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": user_text},
        ]
        r = self.chat(messages, label=label)
        data = extract_json(r.content)
        if data is not None:
            return data

        self.logger.log_event("json_repair", agent=self.name, label=label, raw=r.content[:1500])
        messages.append({"role": "assistant", "content": r.content})
        messages.append({"role": "user", "content": JSON_REPAIR_INSTRUCTION})
        r2 = self.chat(messages, label=f"{label}#repair")
        data = extract_json(r2.content)
        if data is None:
            self.logger.log_event(
                "json_parse_failed", agent=self.name, label=label, raw=r2.content[:1500]
            )
        return data


# ----------------------------------------------------------------
# Planner
# ----------------------------------------------------------------
PLANNER_SYSTEM = """你是 Planner。你的唯一职责是把用户目标拆成一份可执行的步骤清单。

严格输出 JSON，不要输出解释，不要用 markdown 围栏：
{"steps": [{"description": "步骤描述"}, {"description": "步骤描述"}]}

拆解要求：
1. 步骤数越少越好，绝不超过 max_steps 步。
2. 每个步骤必须能靠 read_file / write_file / list_files 三个工具完成或验证。
3. 每个步骤都要能独立验收：描述里写清产物路径和判定标准。
4. 不要出现"分析""思考""整理思路"这类无法验收的步骤。
5. 有先后依赖的，按依赖顺序排列。"""


class Planner(Agent):
    name = "planner"
    tool_names = PLANNER_TOOLS

    def plan(self, goal: str, max_steps: int) -> Plan | None:
        self.system = PLANNER_SYSTEM.replace("max_steps", str(max_steps))
        user = f"用户目标：\n{goal}\n\n请给出不超过 {max_steps} 步的计划。"
        data = self.request_json(user, label="plan")
        return self._to_plan(goal, data, max_steps, revision=0, note="")

    def replan(
        self,
        *,
        goal: str,
        plan: Plan,
        failing_step: Step,
        max_steps: int,
    ) -> Plan | None:
        self.system = PLANNER_SYSTEM.replace("max_steps", str(max_steps))
        completed = "\n".join(
            f"  - 步骤 {s.id}: {s.description}\n    产出：{truncate(s.result, 400)}"
            for s in plan.steps
            if s.done
        ) or "  (无)"

        user = (
            f"总目标：\n{goal}\n\n"
            f"上一版计划（第 {plan.revision + 1} 版）已被评估器判定为**计划本身有问题**，"
            f"不允许局部修补，必须重新拆解。\n\n"
            f"已经确认完成的成果（视为既定事实，不要重做）：\n{completed}\n\n"
            f"卡住的步骤：步骤 {failing_step.id} —— {failing_step.description}\n"
            f"评估器给出的理由：{failing_step.last_feedback or '(未提供)'}\n\n"
            f"请给出一版新计划，修正上述问题。仍然不超过 {max_steps} 步。"
        )
        data = self.request_json(user, label=f"replan_v{plan.revision + 2}")
        return self._to_plan(
            goal,
            data,
            max_steps,
            revision=plan.revision + 1,
            note=failing_step.last_feedback,
        )

    def _to_plan(
        self, goal: str, data: dict | None, max_steps: int, revision: int, note: str
    ) -> Plan | None:
        if not isinstance(data, dict):
            return None

        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            self.logger.log_event("plan_invalid", revision=revision, raw=str(data)[:800])
            return None

        if len(raw_steps) > max_steps:
            # 不静默截断：截断会悄悄丢掉目标的一部分，必须留痕
            self.logger.log_event(
                "plan_truncated", revision=revision, got=len(raw_steps), kept=max_steps
            )

        steps: list[Step] = []
        for item in raw_steps[:max_steps]:
            if isinstance(item, str):
                desc = item.strip()
            elif isinstance(item, dict):
                desc = str(item.get("description") or item.get("step") or "").strip()
            else:
                continue
            if desc:
                # id 由我们重新分配 —— 不信模型给的编号，它经常从 0 开始或者跳号
                steps.append(Step(id=len(steps) + 1, description=desc))

        if not steps:
            self.logger.log_event("plan_invalid", revision=revision, raw=str(data)[:800])
            return None

        return Plan(goal=goal, steps=steps, revision=revision, note=note)


# ----------------------------------------------------------------
# Executor
# ----------------------------------------------------------------
EXECUTOR_SYSTEM = """你是 Executor。你一次只负责执行计划中的**一个**步骤。

规则：
1. 只做「当前步骤」要求的事。不要提前做后面的步骤，不要改动已完成步骤的产物。
2. 需要读写文件时，调用工具。
3. 完成后用不超过 3 句话汇报：做了什么、产物路径、关键结果。
4. 这一步确实做不到时，直接说明原因。不要伪造产物、不要说"已完成"来蒙混过关。"""


class Executor(Agent):
    name = "executor"
    tool_names = EXECUTOR_TOOLS

    def execute(self, *, goal: str, plan: Plan, step: Step, label: str) -> ToolLoop:
        self.system = EXECUTOR_SYSTEM
        return self.run_tools(
            self._user_prompt(goal, plan, step),
            label=label,
            max_rounds=MAX_EXECUTOR_ROUNDS,
        )

    @staticmethod
    def _user_prompt(goal: str, plan: Plan, step: Step) -> str:
        lines = [f"总目标：{goal}", "", "完整计划："]
        for s in plan.steps:
            mark = "✅" if s.done else "⬜"
            lines.append(f"  {mark} 步骤 {s.id}: {s.description}")

        done = [s for s in plan.steps if s.done]
        if done:
            lines += ["", "已完成步骤的产出（仅供参照，不要重做）："]
            for s in done:
                lines.append(f"  - 步骤 {s.id}: {truncate(s.result, 500)}")

        if step.attempts > 0 and step.last_feedback:
            lines += [
                "",
                "⚠️ 你上一次的产出被评估器判为不合格，反馈如下（这次必须改掉）：",
                step.last_feedback,
            ]

        lines += ["", f"现在执行步骤 {step.id}：{step.description}"]
        return "\n".join(lines)


# ----------------------------------------------------------------
# Evaluator
# ----------------------------------------------------------------
EVALUATOR_SYSTEM = """你是 Evaluator。你的职责是**独立核验** Executor 是否真的完成了当前步骤。

最重要的一条：**不要相信 Executor 的自我汇报。**
它说"已写入 report.md"，你必须用 read_file 亲自打开确认内容正确；
它说"共有 3 个来源文件"，你必须用 list_files 亲自数。
工具用完之前不要下结论。

调查完成后，最后一条回复只输出 JSON，不要解释、不要 markdown 围栏：
{"verdict": "pass", "reason": "一句话依据", "feedback": ""}

verdict 只能是这三个值之一：
- pass   ：本章步骤目标已达成，产物存在、内容正确，可以直接进入下一步。feedback 留空。
- revise ：方向正确，但产物有**具体可指出**的缺陷（数字算错、字段缺失、格式不符、
           关键信息未覆盖）。feedback 必须写清"改哪里、改成什么"。
- replan ：计划本身有问题 —— 缺少必要步骤、顺序错误、与前序产物冲突。
           这时不要试图局部修补，直接说清计划哪里错了。

不要因为"措辞不够好""风格不满意"判 revise。只对着当前步骤的验收标准判。"""


class Evaluator(Agent):
    name = "evaluator"
    tool_names = EVALUATOR_TOOLS

    def evaluate(self, *, goal: str, plan: Plan, step: Step) -> Evaluation:
        self.system = EVALUATOR_SYSTEM
        label = f"evaluate_step{step.id}"
        loop = self.run_tools(
            self._user_prompt(goal, plan, step),
            label=label,
            max_rounds=MAX_EVALUATOR_ROUNDS,
        )

        data = extract_json(loop.content)
        if data is None:
            self.logger.log_event(
                "json_repair", agent=self.name, label=label, raw=loop.content[:1500]
            )
            messages = list(loop.messages)
            messages.append({"role": "user", "content": JSON_REPAIR_INSTRUCTION})
            r = self.chat(messages, label=f"{label}#repair")
            data = extract_json(r.content)

        return self._coerce(data, step, loop)

    def _coerce(self, data: dict | None, step: Step, loop: ToolLoop) -> Evaluation:
        """把模型输出强制收敛到 Verdict。

        解析不出来时**不能默认 pass** —— 那等于"评估器坏了就一律放行"。
        默认 revise（保守重做），并把原因记进事件流。
        """
        if data is None:
            self.logger.log_event(
                "evaluator_verdict_fallback", step=step.id, reason="json_unparsable"
            )
            return Evaluation(
                Verdict.REVISE,
                "评估器未返回可解析的判定",
                "评估器没能给出结构化判定。请重新检查本步骤的产物是否真的符合验收标准。",
            )

        raw_verdict = str(data.get("verdict", "")).strip().lower()
        alias = {"pass": Verdict.PASS, "revise": Verdict.REVISE, "replan": Verdict.REPLAN}
        verdict = alias.get(raw_verdict)
        if verdict is None:
            self.logger.log_event(
                "evaluator_verdict_fallback", step=step.id, reason=f"unknown:{raw_verdict!r}"
            )
            verdict = Verdict.REVISE

        if not loop.stopped and verdict is Verdict.PASS:
            # 调查循环被 max_rounds 掐断，最后的 JSON 是在证据不完整时给的 —— 不放行
            self.logger.log_event("evaluator_pass_without_stop", step=step.id)
            verdict = Verdict.REVISE

        return Evaluation(
            verdict=verdict,
            reason=str(data.get("reason", "")).strip(),
            feedback=str(data.get("feedback", "")).strip(),
        )

    @staticmethod
    def _user_prompt(goal: str, plan: Plan, step: Step) -> str:
        lines = [
            f"总目标：{goal}",
            f"计划共 {len(plan.steps)} 步，当前是第 {step.id} 步"
            f"（这是第 {step.attempts} 次尝试）。",
            "",
            f"当前步骤的验收目标：{step.description}",
            "",
            "Executor 的自我汇报（不可信，必须自己动手核实）：",
            truncate(step.result, 1500),
        ]
        if step.attempts > 1 and step.last_feedback:
            lines += ["", "注意：这是重做后的产物。上一轮的反馈是：", step.last_feedback]
        return "\n".join(lines)
