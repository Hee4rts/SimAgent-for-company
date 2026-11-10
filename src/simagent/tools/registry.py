from __future__ import annotations

from ..contracts import ToolDescriptor
from .base import ModelTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ModelTool] = {}

    def register(self, tool: ModelTool) -> None:
        name = tool.descriptor.name
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> ModelTool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"Unknown model tool: {name}") from None

    def catalog(self) -> list[ToolDescriptor]:
        return [tool.descriptor.model_copy(deep=True) for tool in self._tools.values()]
