# coding: utf-8
"""计划解析的单测：模型给的计划必须先过"可执行性"这一关。

_to_plan 是 Planner 与状态机之间的合同：
- 步骤 id 由我们重新分配（模型经常从 0 开始、跳号、或者给字符串 id）
- 超过 max_steps 的步骤不静默丢弃，而是留痕后截断
- 一份"解析成功但没有可用步骤"的计划，等同于没有计划
"""
from __future__ import annotations

from multi_agent.agents import Planner
from multi_agent.logger import RunLogger


def make_planner() -> Planner:
    return Planner(llm=None, logger=RunLogger(goal="g", model="test", mode="test"))


def test_renumbers_step_ids_from_one():
    planner = make_planner()
    plan = planner._to_plan(
        "g",
        {"steps": [{"id": 0, "description": "a"}, {"id": 7, "description": "b"}]},
        max_steps=6,
        revision=0,
        note="",
    )
    assert [s.id for s in plan.steps] == [1, 2]
    assert [s.description for s in plan.steps] == ["a", "b"]


def test_accepts_plain_string_steps():
    plan = make_planner()._to_plan("g", {"steps": ["读文件", "写报告"]}, 6, 0, "")
    assert [s.description for s in plan.steps] == ["读文件", "写报告"]


def test_truncates_to_max_steps():
    data = {"steps": [{"description": f"s{i}"} for i in range(10)]}
    plan = make_planner()._to_plan("g", data, max_steps=3, revision=0, note="")
    assert len(plan.steps) == 3


def test_falls_back_to_step_key():
    plan = make_planner()._to_plan("g", {"steps": [{"step": "用 step 键"}]}, 6, 0, "")
    assert plan.steps[0].description == "用 step 键"


def test_rejects_plan_without_usable_steps():
    planner = make_planner()
    assert planner._to_plan("g", {"steps": []}, 6, 0, "") is None
    assert planner._to_plan("g", {"steps": [{"description": "  "}]}, 6, 0, "") is None
    assert planner._to_plan("g", {"steps": "不是列表"}, 6, 0, "") is None
    assert planner._to_plan("g", {}, 6, 0, "") is None
    assert planner._to_plan("g", None, 6, 0, "") is None


def test_new_plan_starts_all_pending():
    plan = make_planner()._to_plan("g", {"steps": [{"description": "a"}]}, 6, revision=2, note="x")
    assert plan.revision == 2
    assert plan.note == "x"
    assert plan.steps[0].attempts == 0
    assert plan.steps[0].verdict is None
    assert not plan.steps[0].done
