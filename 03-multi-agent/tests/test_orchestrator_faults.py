# coding: utf-8
"""故障注入测试：用假的 LLM 客户端把状态机的**失败路径**全部走一遍。

这是 03 章"显式状态机"这个设计选择的第二份证据（第一份是 test_state.py）。

真实的失败长什么样
------------------
跑真实任务时，失败是**偶发**的：模型大部分时候正常，偶尔给出不可解析的 JSON，
偶尔评估器和执行器互相踢皮球。用真 LLM 复现这些路径，要么靠运气，要么靠烧钱。

而这里用一个 scripted 的假客户端，把"评估器永远说 revise""Planner 永远返回废话"
变成两行代码。于是这些结论可以被**断言**，而不是靠一次运行的日志去猜：

- 评估器永远说 revise  ->  重试 -> 升级重规划 -> 触顶 ABORT（不会死循环）
- Planner 永远给废话   ->  一次格式修复 -> 仍失败则 ABORT（不会解析出空计划当成功）
- 评估器给不可解析输出 ->  降级为 revise（**绝不默认 pass**）
- 单次 dispatch 内超预算 ->  守卫在调用前抛出，转成 ABORT（不会先花完再说）

最后一条尤其重要：预算守卫必须挂在**每一次 LLM 调用之前**。
挂在"每轮结束后"的话，一个 6 轮的工具循环可以一口气把预算超掉 6 倍。
"""
from __future__ import annotations

import pytest

from multi_agent.agents import Evaluator, Executor, Planner
from multi_agent.logger import RunLogger
from multi_agent.orchestrator import Orchestrator
from multi_agent.state import Budget, Verdict

# ----------------------------------------------------------------
# 假 LLM 客户端
# ----------------------------------------------------------------


class _Function:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, id_: str, name: str, arguments: str):
        self.id = id_
        self.function = _Function(name, arguments)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


class _Message:
    def __init__(self, content: str, tool_calls: list[_ToolCall]):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message: _Message, finish_reason: str):
        self.message = message
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, n: int):
        self.prompt_tokens = n
        self.completion_tokens = n
        self.total_tokens = n


class _Response:
    def __init__(self, content: str, tool_calls: list[_ToolCall], finish_reason: str, tokens: int):
        self.choices = [_Choice(_Message(content, tool_calls), finish_reason)]
        self.usage = _Usage(tokens)


class FakeLLM:
    """按脚本逐条返回。脚本用尽即报错 —— 测试里"少写了一条回复"必须是硬失败。"""

    model = "fake-model"

    def __init__(self, script: list, tokens_per_call: int = 10):
        self.script = list(script)
        self.tokens_per_call = tokens_per_call
        self.calls: list[dict] = []

    def call(self, messages, tools=None, temperature=None, max_retries=0, on_retry=None):
        self.calls.append({"tools": tools, "messages": messages})
        if not self.script:
            raise AssertionError(
                f"FakeLLM 脚本用尽：第 {len(self.calls)} 次调用没有对应回复"
            )
        item = self.script.pop(0)

        if isinstance(item, str):
            return _Response(item, [], "stop", self.tokens_per_call)

        if isinstance(item, dict):
            # 用来构造"带 tool_calls 但 finish_reason 不是 tool_calls"这种服务端怪癖
            tool_calls = [
                _ToolCall(f"call_{i}", name, args)
                for i, (name, args) in enumerate(item.get("tool_calls", []))
            ]
            return _Response(
                item.get("content", ""),
                tool_calls,
                item.get("finish_reason", "stop"),
                self.tokens_per_call,
            )

        tool_calls = [
            _ToolCall(f"call_{i}", name, args) for i, (name, args) in enumerate(item)
        ]
        return _Response("", tool_calls, "tool_calls", self.tokens_per_call)


# ----------------------------------------------------------------
# 脚本素材
# ----------------------------------------------------------------
PLAN_OK = '{"steps": [{"description": "做一件事"}]}'
PLAN_OK_V2 = '{"steps": [{"description": "换个思路做这件事"}]}'
NOT_JSON = "我觉得这个任务可以这样拆……（此处省略一百字）"
READ_CALL = [("read_file", '{"path": "nope.txt"}')]


def verdict(kind: str, feedback: str = "再改一下") -> str:
    return f'{{"verdict": "{kind}", "reason": "测试", "feedback": "{feedback}"}}'


def run_script(script: list, budget: Budget | None = None, goal: str = "测试目标"):
    llm = FakeLLM(script)
    logger = RunLogger(goal=goal, model="fake-model", mode="test")
    orchestrator = Orchestrator(llm=llm, logger=logger, budget=budget or Budget())
    state = orchestrator.run(goal, verbose=False)
    return state, logger, llm


# ----------------------------------------------------------------
# 1. 正常路径也要能跑通（否则失败路径的断言没有意义）
# ----------------------------------------------------------------
def test_happy_path_passes_in_three_calls():
    state, logger, _ = run_script([PLAN_OK, "做完了", verdict("pass", "")])
    assert state.status == "success"
    assert state.plan.steps[0].status == "passed"
    assert logger.llm_calls == 3
    assert [c["agent"] for c in logger.calls] == ["planner", "executor", "evaluator"]


# ----------------------------------------------------------------
# 2. 评估器永远说 revise
# ----------------------------------------------------------------
def test_endless_revise_terminates_instead_of_looping_forever():
    """评估器不让过 -> 重试一次 -> 触顶 ABORT。必须有终点。"""
    state, logger, _ = run_script(
        [PLAN_OK, "第一次", verdict("revise"), "第二次", verdict("revise")],
        budget=Budget(max_attempts_per_step=2, max_replans=0),
    )
    assert state.status == "failed"
    assert state.abort_reason.startswith("attempts_exhausted")
    assert state.plan.steps[0].attempts == 2
    # planner 1 次 + executor 2 次 + evaluator 2 次
    assert logger.llm_calls == 5


def test_replan_verdict_rebuilds_the_plan_then_can_succeed():
    state, logger, _ = run_script(
        [
            PLAN_OK,
            "做完了",
            verdict("replan", "计划缺少必要步骤"),
            PLAN_OK_V2,  # 重新规划
            "这次做完了",
            verdict("pass", ""),
        ],
        budget=Budget(max_replans=1),
    )
    assert state.status == "success"
    assert state.replans == 1
    assert state.plan.revision == 1
    assert state.plan.steps[0].description == "换个思路做这件事"
    assert any(e["type"] == "plan_replanned" for e in logger.events)


def test_replan_budget_exhausted_aborts_cleanly():
    state, _, _ = run_script(
        [PLAN_OK, "做完了", verdict("replan")],
        budget=Budget(max_replans=0),
    )
    assert state.status == "failed"
    assert state.abort_reason.startswith("replan_exhausted")


def test_tool_calls_are_executed_even_when_finish_reason_is_stop():
    """有的服务带 tool_calls 时仍返回 finish_reason="stop"。

    01/02 的写法是 `if finish_reason == "tool_calls"` 才执行工具。
    在那个判断下，这批 tool_calls 会被丢掉，而 assistant 消息已经带着它们进了上下文
    —— 下一次请求就会出现"有 tool_call 没有对应 tool 结果"的错配。
    所以这里改成看 `msg.tool_calls` 是否为空，实测这条路径能正常执行工具。
    """
    script = [
        PLAN_OK,
        {
            "content": "",
            "tool_calls": [("read_file", '{"path": "nope.txt"}')],
            "finish_reason": "stop",
        },
        "读完了，文件不存在",
        verdict("pass", ""),
    ]
    state, logger, _ = run_script(script)
    assert state.status == "success"
    assert any(e["type"] == "tool_call" for e in logger.events)
    # 工具循环没有在第一批 tool_calls 上提前退出，而是多要了一轮汇报
    assert logger.llm_calls == 4


# ----------------------------------------------------------------
# 3. Planner 给废话
# ----------------------------------------------------------------
def test_planner_garbage_triggers_one_repair_attempt():
    """第一次输出不是 JSON -> 再问一次 -> 拿到合法计划，流程继续。"""
    state, logger, llm = run_script([NOT_JSON, PLAN_OK, "做完了", verdict("pass", "")])
    assert state.status == "success"
    assert any(e["type"] == "json_repair" for e in logger.events)
    assert logger.llm_calls == 4  # 多出来的那次就是格式修复


def test_planner_persistently_garbage_aborts_without_fake_success():
    state, logger, _ = run_script([NOT_JSON, NOT_JSON])
    assert state.status == "failed"
    assert state.abort_reason == "planner_no_valid_plan"
    assert state.plan is None
    assert any(e["type"] == "json_parse_failed" for e in logger.events)


def test_plan_with_no_usable_steps_is_rejected():
    """`{"steps": []}` 是"合法 JSON 但非法计划" —— 不能当成计划用。"""
    state, _, _ = run_script(['{"steps": []}', '{"steps": []}'])
    assert state.status == "failed"
    assert state.abort_reason == "planner_no_valid_plan"


# ----------------------------------------------------------------
# 4. 评估器输出不可解析
# ----------------------------------------------------------------
def test_unparsable_verdict_degrades_to_revise_not_pass():
    """评估器坏了的时候，绝不能默认放行。"""
    state, logger, _ = run_script(
        [PLAN_OK, "做完了", "这不是 JSON", NOT_JSON],
        budget=Budget(max_attempts_per_step=1, max_replans=0),
    )
    assert state.status == "failed"
    assert state.abort_reason.startswith("attempts_exhausted")
    assert any(e["type"] == "evaluator_verdict_fallback" for e in logger.events)


def test_unknown_verdict_string_degrades_to_revise():
    state, logger, _ = run_script(
        [PLAN_OK, "做完了", '{"verdict": "maybe", "reason": "?"}'],
        budget=Budget(max_attempts_per_step=1, max_replans=0),
    )
    assert state.plan.steps[0].verdict is Verdict.REVISE
    assert any(
        e["type"] == "evaluator_verdict_fallback" and "unknown" in e["reason"]
        for e in logger.events
    )


# ----------------------------------------------------------------
# 5. 预算守卫挂在"每一次调用之前"
# ----------------------------------------------------------------
def test_budget_guard_fires_inside_a_single_tool_loop():
    """max_llm_calls=3：planner 用掉 1 次，executor 第 3 次调用时守卫拦下。

    如果守卫写在"每轮循环结束后"，这里会先花掉 6 次才停。
    """
    script = [PLAN_OK, READ_CALL, READ_CALL, READ_CALL, READ_CALL]
    state, logger, llm = run_script(script, budget=Budget(max_llm_calls=3))
    assert state.status == "failed"
    assert state.abort_reason.startswith("budget_exceeded")
    assert logger.llm_calls == 3
    assert len(llm.calls) == 3


def test_llm_call_budget_aborts_before_next_dispatch():
    state, logger, _ = run_script(
        [PLAN_OK, "做完了", verdict("pass", "")],
        budget=Budget(max_llm_calls=2),
    )
    assert state.status == "failed"
    assert state.abort_reason.startswith("llm_calls_exhausted")
    assert logger.llm_calls == 2


def test_token_budget_aborts():
    llm = FakeLLM([PLAN_OK, "做完了", verdict("pass", "")], tokens_per_call=5000)
    logger = RunLogger(goal="g", model="fake-model", mode="test")
    orchestrator = Orchestrator(
        llm=llm, logger=logger, budget=Budget(max_total_tokens=6000)
    )
    state = orchestrator.run("g", verbose=False)
    assert state.status == "failed"
    assert "tokens" in state.abort_reason


# ----------------------------------------------------------------
# 6. 工具集权限：从调用记录上验证，而不是相信 prompt
# ----------------------------------------------------------------
def test_evaluator_never_receives_a_write_tool():
    _, logger, llm = run_script([PLAN_OK, "做完了", verdict("pass", "")])
    for record, call in zip(logger.calls, llm.calls, strict=True):
        if record["agent"] != "evaluator":
            continue
        names = {t["function"]["name"] for t in (call["tools"] or [])}
        assert names == {"read_file", "list_files"}


def test_planner_gets_no_tools_at_all():
    _, logger, llm = run_script([PLAN_OK, "做完了", verdict("pass", "")])
    for record, call in zip(logger.calls, llm.calls, strict=True):
        if record["agent"] == "planner":
            assert not call["tools"]


# ----------------------------------------------------------------
# 7. 状态机的保险丝
# ----------------------------------------------------------------
def test_iteration_fuse_prevents_hang(monkeypatch):
    """即使 decide() 被改坏、动作不改变状态，也要在有限步内退出。"""
    import multi_agent.orchestrator as orch_module

    llm = FakeLLM([PLAN_OK] * 50)
    logger = RunLogger(goal="g", model="fake-model", mode="test")
    orchestrator = orch_module.Orchestrator(llm=llm, logger=logger, budget=Budget())

    # 让 decide 永远返回 PLAN，且 _do_plan 不改变状态
    monkeypatch.setattr(
        orch_module, "decide", lambda state, budget: orch_module.Decision(
            orch_module.Action.PLAN, reason="stuck"
        )
    )
    monkeypatch.setattr(Orchestrator, "_do_plan", lambda self, state, decision, verbose: None)
    orchestrator.max_iterations = 7

    state = orchestrator.run("g", verbose=False)
    assert state.status == "failed"
    assert state.abort_reason == "iteration_fuse(7)"


# ----------------------------------------------------------------
# 8. 三个角色的类属性就是契约
# ----------------------------------------------------------------
@pytest.mark.parametrize(
    "cls, expected",
    [
        (Planner, set()),
        (Executor, {"read_file", "write_file", "list_files"}),
        (Evaluator, {"read_file", "list_files"}),
    ],
)
def test_role_tool_contract(cls, expected):
    assert cls.tool_names == expected
