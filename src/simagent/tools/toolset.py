"""工具注册入口：使用 Pydantic AI 官方 FunctionToolset。

在此导入业务函数，使用 toolset.add_function 或 toolset.add_tool 注册；
也可以直接使用 @toolset.tool_plain / @toolset.tool 定义工具。
独立模块中的装饰器从 .toolset 导入 toolset，并在本文件创建对象后导入该模块。
"""

from pydantic_ai import FunctionToolset

from ..infrastructure.dependencies import Services

toolset = FunctionToolset[Services]()

# 在此添加业务函数的导入和官方工具注册。
