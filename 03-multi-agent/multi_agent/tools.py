# coding: utf-8
"""工具注册机制 + 内置工具（从 01 章拷贝，加了一个工具集过滤）。

与 01 的差异只有一处：``get_tool_schemas(only=...)``。
03 章里三个 Agent 的工具集**不一样**，工具集就是权限边界：

    Planner    无工具           —— 它只产出计划文本，不需要碰文件系统
    Executor   read/write/list  —— 它要产出文件
    Evaluator  read/list        —— 它只能看，不能改

如果 Evaluator 有 write 权限，它"验证不通过"时就可能自己动手改产物，
评估就退化成"自评自改"。**工具的裁剪是一种结构性约束，比 prompt 里写
"请不要修改文件"可靠得多。**
"""
from __future__ import annotations

import json
from pathlib import Path

WORKSPACE = Path("./workspace")

# name -> {"fn": callable, "schema": dict}
TOOLS: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict):
    """装饰器：把函数注册成 LLM 可调用的工具。

    parameters 遵循 JSON Schema：
    {"type": "object", "properties": {...}, "required": [...]}
    """
    def decorator(fn):
        TOOLS[name] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
        }
        return fn
    return decorator


def get_tool_schemas(only: set[str] | None = None) -> list[dict]:
    """返回工具 schema。only 非空时只返回指定工具 —— 用来给不同 Agent 裁剪权限。"""
    if only is None:
        return [t["schema"] for t in TOOLS.values()]
    return [t["schema"] for name, t in TOOLS.items() if name in only]


def execute_tool(name: str, args: dict) -> str:
    """执行工具。所有异常转为字符串返回给 LLM，不让程序崩溃。

    与 01 的差异：参数解析从工具函数上移到调用方（02 章的做法，
    GLM 空参可能返回 "" / "{}" / "null"），这里只收 dict。
    """
    if name not in TOOLS:
        return f"Error: unknown tool '{name}'"

    fn = TOOLS[name]["fn"]
    try:
        return str(fn(**args))
    except Exception as e:
        return f"Error calling {name}: {type(e).__name__}: {e}"


def parse_tool_args(raw: str | None) -> dict:
    """把模型给的 arguments 字符串解析成 dict。非法就退化成空参数，由工具自己报错。"""
    if not raw:
        return {}
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


# ============================================================
# 内置工具
# ============================================================
@tool(
    name="read_file",
    description="读取 workspace 目录下的文件内容。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对路径，如 'notes.txt'"},
        },
        "required": ["path"],
    },
)
def read_file(path: str) -> str:
    full = WORKSPACE / path
    if not full.exists():
        return f"File not found: {path}"
    return full.read_text(encoding="utf-8")


@tool(
    name="write_file",
    description="把内容写入 workspace 下的文件（覆盖写入）。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对路径，如 'notes.txt'"},
            "content": {"type": "string", "description": "要写入的内容"},
        },
        "required": ["path", "content"],
    },
)
def write_file(path: str, content: str) -> str:
    full = WORKSPACE / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


@tool(
    name="list_files",
    description="列出 workspace 目录下的所有文件。",
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_files() -> str:
    if not WORKSPACE.exists():
        return "(workspace is empty)"
    files = [str(p.relative_to(WORKSPACE)) for p in WORKSPACE.rglob("*") if p.is_file()]
    return "\n".join(files) if files else "(no files)"
