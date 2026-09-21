# coding: utf-8
"""01 的 tools.py 改造成 MCP server。

对比原版，变了三处：
  1. 装饰器：@tool(name=..., description=..., parameters=...) → @mcp.tool(...)
  2. schema：手写的 parameters dict 整个删掉，SDK 从类型注解 + Field 生成
  3. 描述：函数外的 description 参数 → @mcp.tool(description=...) 显式传入

函数体一个字没改。

为什么用显式 description 而不是 docstring：
  MCP v2 SDK 默认把整个 docstring（含缩进）塞进工具的 description 字段，
  且**不解析** Args / Returns 段。两个后果：
    - docstring 里的 4 空格缩进会被原样发送，白烧 token
    - 参数层面的语义（如"相对路径还是绝对路径"）传不到 LLM

  改用：
    - @mcp.tool(description=...)            → 工具描述，给 LLM 看
    - Annotated[str, Field(description=...)] → 参数描述，给 LLM 看
    - docstring                              → 保留给读代码的人，不发给 LLM

  显式传 description 后，SDK 会用参数值覆盖 docstring，两者互不冲突。
"""
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

mcp = MCPServer("filesystem-minimal")

# 沙箱目录。所有文件操作限制在此目录内。
# 相对路径解析基于 server 进程的 cwd —— client 通过 subprocess 启动时，
# cwd 是 client 的工作目录，所以实际位置取决于从哪启动 client。
WORKSPACE = Path("./workspace")


@mcp.tool(
    description=(
        "读取 workspace 目录下的文件内容。"
        "文件不存在时返回 'File not found: <path>'，不会抛异常。"
    ),
)
def read_file(
    path: Annotated[
        str,
        Field(description="相对于 workspace 的文件路径，如 'notes.txt'。不要传绝对路径。"),
    ],
) -> str:
    """读取文件内容。返回值：文件全文；不存在时为 'File not found: <path>'。"""
    full = WORKSPACE / path
    if not full.exists():
        return f"File not found: {path}"
    return full.read_text(encoding="utf-8")


@mcp.tool(
    description=(
        "把内容覆盖写入 workspace 下的文件。"
        "父目录不存在会自动创建；目标文件已存在会完全覆盖。"
        "要追加内容，请先 read_file 再 write_file 拼接。"
    ),
)
def write_file(
    path: Annotated[
        str,
        Field(description="相对于 workspace 的文件路径，如 'notes.txt'。"),
    ],
    content: Annotated[
        str,
        Field(description="要写入的完整文本内容（覆盖写入，不是追加）。"),
    ],
) -> str:
    """覆盖写文件。返回值：'Wrote <N> chars to <path>'。"""
    full = WORKSPACE / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


@mcp.tool(
    description=(
        "递归列出 workspace 目录下的所有文件（含子目录）。"
        "每行一个相对路径。常用于 read_file 前确认文件是否存在。"
    ),
)
def list_files() -> str:
    """列出所有文件。返回值：每行一个相对路径；空目录为 '(no files)'。"""
    if not WORKSPACE.exists():
        return "(workspace is empty)"
    files = [str(p.relative_to(WORKSPACE)) for p in WORKSPACE.rglob("*") if p.is_file()]
    return "\n".join(files) if files else "(no files)"


if __name__ == "__main__":
    WORKSPACE.mkdir(exist_ok=True)
    # stdio 传输：server 通过 stdin/stdout 收发换行分隔的 JSON-RPC 报文。
    # 启动后 stdout 属于协议专用，不要往 stdout 打任何非协议内容，
    # 否则会污染帧。要调试请用 stderr 或 logging。
    mcp.run(transport="stdio")