# coding: utf-8
"""工具注册机制 + 内置工具。"""
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


def get_tool_schemas():
    """返回所有工具 schema，供 LLM 使用。"""
    return [t["schema"] for t in TOOLS.values()]


def execute_tool(name: str, arguments: str) -> str:
    """执行工具。所有异常转为字符串返回给 LLM，不让程序崩溃。"""
    if name not in TOOLS:
        return f"Error: unknown tool '{name}'"

    fn = TOOLS[name]["fn"]
    try:
        args = json.loads(arguments) if arguments else {}
        return str(fn(**args))
    except Exception as e:
        return f"Error calling {name}: {type(e).__name__}: {e}"


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