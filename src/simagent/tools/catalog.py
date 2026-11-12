"""将官方 FunctionToolset 的当前注册结果导出为可读的工具清单。"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic_ai import FunctionToolset

CATALOG_PATH = Path(__file__).with_name("registered_tools.md")


def write_tool_catalog(
    toolset: FunctionToolset[Any], path: Path = CATALOG_PATH,
) -> Path:
    """只读取已注册的定义，不执行工具、不读取密钥，也不调用 prepare。"""
    registered = sorted(tuple(toolset.tools.values()), key=lambda tool: tool.name)
    lines = ["# 已注册工具", ""]
    for tool in registered:
        description = ((tool.description or "").strip() or "未提供说明").splitlines()[0]
        lines.extend([
            f"工具：`{tool.name}`  ",
            f"作用：{description}",
            "",
        ])
    content = "\n".join(lines) + "\n"
    try:
        if path.read_text(encoding="utf-8") == content:
            return path
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.stem}-", suffix=".tmp", delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


if __name__ == "__main__":
    from .toolset import toolset

    print(write_tool_catalog(toolset))
