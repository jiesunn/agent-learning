# coding: utf-8
"""MCP server exposing the same 3 file tools as 01, with English descriptions.

Why English descriptions:
  Chinese descriptions cost ~2x more tokens on DeepSeek and ~1.7x on GLM.
  LLM tool selection is not measurably worse with English tool descriptions,
  even when the user prompt is Chinese.

  This file exists to quantify that: run scripts/measure_tool_cost.py
  against this server and compare with the Chinese version.
"""
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

mcp = MCPServer("filesystem-minimal")

WORKSPACE = Path("./workspace")


@mcp.tool(
    description=(
        "Read a file from the workspace directory. "
        "Returns 'File not found: <path>' if the file does not exist. "
        "Never raises an exception."
    ),
)
def read_file(
    path: Annotated[
        str,
        Field(description="Path relative to workspace, e.g. 'notes.txt'. Do not use absolute paths."),
    ],
) -> str:
    """Read file contents. Returns full text, or 'File not found: <path>'."""
    full = WORKSPACE / path
    if not full.exists():
        return f"File not found: {path}"
    return full.read_text(encoding="utf-8")


@mcp.tool(
    description=(
        "Write content to a file in the workspace, overwriting any existing content. "
        "Parent directories are created automatically. "
        "To append, read the file first and write back the concatenated result."
    ),
)
def write_file(
    path: Annotated[
        str,
        Field(description="Path relative to workspace, e.g. 'notes.txt'."),
    ],
    content: Annotated[
        str,
        Field(description="Full text content to write (overwrite, not append)."),
    ],
) -> str:
    """Overwrite a file. Returns 'Wrote <N> chars to <path>'."""
    full = WORKSPACE / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


@mcp.tool(
    description=(
        "Recursively list all files in the workspace directory, including subdirectories. "
        "One relative path per line. "
        "Use this before read_file to check whether a file exists."
    ),
)
def list_files() -> str:
    """List all files. Returns one relative path per line, or '(no files)' if empty."""
    if not WORKSPACE.exists():
        return "(workspace is empty)"
    files = [str(p.relative_to(WORKSPACE)) for p in WORKSPACE.rglob("*") if p.is_file()]
    return "\n".join(files) if files else "(no files)"


if __name__ == "__main__":
    mcp.run(transport="stdio")