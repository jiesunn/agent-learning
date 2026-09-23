# coding: utf-8
"""模型输出解析单测。

03 章里 Planner 和 Evaluator 的全部输出都是 JSON，而且这个 JSON 直接决定控制流。
所以解析器的每条降级路径都要有用例顶着。
"""
from __future__ import annotations

import pytest

from multi_agent.parsing import extract_json, strip_fences


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict": "pass"}',
        '  {"verdict": "pass"}  ',
        '```json\n{"verdict": "pass"}\n```',
        '```\n{"verdict": "pass"}\n```',
        '好的，我的判断如下：\n{"verdict": "pass"}',
        '{"verdict": "pass"}\n希望对你有帮助。',
    ],
)
def test_extracts_dict_from_common_shapes(text):
    assert extract_json(text) == {"verdict": "pass"}


def test_extracts_multi_field_payload_after_fence_and_chatter():
    text = '```json\n{"verdict": "pass", "reason": "文件存在"}\n```\n以上。'
    assert extract_json(text) == {"verdict": "pass", "reason": "文件存在"}


def test_handles_nested_objects():
    text = '前言 {"plan": {"steps": [{"description": "a"}]}} 后记'
    assert extract_json(text) == {"plan": {"steps": [{"description": "a"}]}}


def test_brace_inside_string_does_not_close_object():
    """`{"note": "a } b"}` 里的 } 不是闭合符 —— 朴素实现会在这里截断。"""
    assert extract_json('{"note": "a } b"}') == {"note": "a } b"}


def test_escaped_quote_inside_string():
    assert extract_json(r'{"note": "a \" b"}')


def test_first_valid_object_wins_when_prose_contains_braces():
    text = '格式是 {"verdict": ...}，实际结果：{"verdict": "revise"}'
    assert extract_json(text) == {"verdict": "revise"}


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "这里根本没有 JSON",
        '{"verdict": "pass",}',  # 尾逗号：不宽容，宁可报错也不猜
        "[1, 2, 3]",  # 顶层不是对象
        '"just a string"',
    ],
)
def test_returns_none_on_unparsable(text):
    assert extract_json(text) is None


def test_strip_fences_without_fence_is_identity():
    assert strip_fences(' {"a": 1} ') == '{"a": 1}'
