# coding: utf-8
"""工具集 = 权限边界。这个文件把这条设计原则钉成断言。"""
from __future__ import annotations

import pytest

from multi_agent.agents import EVALUATOR_TOOLS, EXECUTOR_TOOLS, PLANNER_TOOLS
from multi_agent.tools import execute_tool, get_tool_schemas, parse_tool_args


def names(only: set[str]) -> set[str]:
    return {s["function"]["name"] for s in get_tool_schemas(only)}


def test_evaluator_cannot_write():
    """评估器没有写权限 —— 它是"不能改"，不是"被要求不要改"。"""
    assert "write_file" not in names(EVALUATOR_TOOLS)


def test_executor_can_write():
    assert names(EXECUTOR_TOOLS) == {"read_file", "write_file", "list_files"}


def test_planner_has_no_tools():
    assert names(PLANNER_TOOLS) == set()
    assert get_tool_schemas(PLANNER_TOOLS) == []


def test_none_means_all_tools():
    assert names(None) == {"read_file", "write_file", "list_files"}  # type: ignore[arg-type]


def test_schema_shape_is_openai_compatible():
    for schema in get_tool_schemas():
        assert schema["type"] == "function"
        assert set(schema["function"]) == {"name", "description", "parameters"}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"path": "a.txt"}', {"path": "a.txt"}),
        (None, {}),
        ("", {}),
        ("不是 JSON", {}),
        ('["列表不是对象"]', {}),
        ("null", {}),
    ],
)
def test_parse_tool_args_never_raises(raw, expected):
    assert parse_tool_args(raw) == expected


def test_unknown_tool_returns_error_string_instead_of_raising():
    assert execute_tool("nope", {}).startswith("Error: unknown tool")


def test_tool_internal_error_is_returned_as_string():
    # write_file 缺少必填参数 -> TypeError，但不应该冒泡到主循环
    assert execute_tool("write_file", {}).startswith("Error calling write_file")
