"""工具注册入口：使用 Pydantic AI 官方 FunctionToolset。

在此导入业务函数，使用 toolset.add_function 或 toolset.add_tool 注册；
也可以直接使用 @toolset.tool_plain / @toolset.tool 定义工具。
独立模块中的装饰器从 .toolset 导入 toolset，并在本文件创建对象后导入该模块。
已注册工具清单在启动网页时自动写入同目录 registered_tools.md。
"""

from pydantic_ai import FunctionToolset, Tool

from ..infrastructure.dependencies import Services
from .serpapi_search import web_search
from .leave_approval.api import APPROVAL_TOOLS

toolset = FunctionToolset[Services]()
toolset.add_tool(Tool(web_search, name="web_search", timeout=30))

for function in APPROVAL_TOOLS:
    toolset.add_tool(Tool(
        function,
        metadata={"simagent": {"version": "1", "tags": ["leave_approval"], "idempotent": True}},
    ))
