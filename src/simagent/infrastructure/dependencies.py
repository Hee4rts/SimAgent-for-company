"""Application-owned data, knowledge/KG/Skills and simulation clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import JsonValue

from ..schemas import DataReference, ToolInput


class DataProvider(Protocol):
    async def read(self, reference: DataReference) -> JsonValue: ...


class KnowledgeProvider(Protocol):
    async def search(self, query: str, references: list[str]) -> list[dict[str, JsonValue]]: ...


@dataclass(frozen=True)
class Services:
    """Application dependencies for native RunContext[Services] tools.

    The executor supplies input/idempotency_key on a per-call copy. Agent calls
    normally use only data, knowledge and clients; clients are application-owned.
    """

    data: DataProvider | None = None
    knowledge: KnowledgeProvider | None = None
    clients: dict[str, Any] = field(default_factory=dict)
    input: ToolInput | None = None
    idempotency_key: str | None = None
