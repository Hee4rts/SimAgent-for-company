"""Application-owned data, knowledge/KG/Skills and simulation clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import JsonValue

from .contracts import DataReference


class DataProvider(Protocol):
    async def read(self, reference: DataReference) -> JsonValue: ...


class KnowledgeProvider(Protocol):
    async def search(self, query: str, references: list[str]) -> list[dict[str, JsonValue]]: ...


@dataclass(frozen=True)
class Services:
    data: DataProvider | None = None
    knowledge: KnowledgeProvider | None = None
    clients: dict[str, Any] = field(default_factory=dict)
