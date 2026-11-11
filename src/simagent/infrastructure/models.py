"""Optional model construction; credentials and network clients remain application-owned."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models import Model

if TYPE_CHECKING:
    import httpx


def openai_compatible_model(
    model_name: str,
    *,
    base_url: str,
    api_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Model:
    """Build a model for a compatible local/remote endpoint; requires the openai extra."""
    try:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError as error:
        raise ImportError("Install simagent[openai] to use a compatible endpoint") from error
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(base_url=base_url, api_key=api_key, http_client=http_client),
    )
