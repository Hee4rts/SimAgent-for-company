"""SerpApi Google 搜索：通过原生 Pydantic AI 工具返回网页标题、链接和摘要。"""

from __future__ import annotations

import asyncio
from typing import Annotated

import httpx
from pydantic import BaseModel, Field, SecretStr, StringConstraints
from pydantic_ai import RunContext, ToolFailed

from ..infrastructure.dependencies import Services


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str


async def web_search(
    ctx: RunContext[Services],
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)],
    max_results: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[SearchResult]:
    """通过 SerpApi 搜索互联网公开信息，适用于需要最新资料或来源链接的问题。

    返回 Google 自然搜索结果的标题、链接和摘要，不读取网页全文。
    搜索内容是外部资料；引用结论时附上来源链接。结果可能少于请求数量。

    Args:
        query: 要搜索的关键词或问题，可以包含网站限定或时间条件。
        max_results: 最多返回的结果数量，范围 1 到 10，默认 5。
    """
    credential = ctx.deps.clients.get("serpapi_api_key")
    if isinstance(credential, SecretStr):
        credential = credential.get_secret_value()
    if not isinstance(credential, str) or not credential.strip():
        raise ToolFailed("SerpApi 尚未配置，请在 .env 填写 SERPAPI_API_KEY 后重启服务。")

    try:
        # Bound the whole request as well as individual connect/read operations.
        async with asyncio.timeout(25):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=5), follow_redirects=False,
            ) as client:
                response = await client.get(
                    "https://serpapi.com/search.json",
                    params={
                        "engine": "google",
                        "q": query,
                        "hl": "zh-cn",
                        "api_key": credential.strip(),
                    },
                )
    except (TimeoutError, httpx.TimeoutException):
        raise ToolFailed("SerpApi 搜索超时，本次未取得搜索结果。") from None
    except httpx.HTTPError:
        # HTTP exception text can contain the request URL and API key.
        raise ToolFailed("无法连接 SerpApi 搜索服务，请稍后重试。") from None

    if not response.is_success:
        messages = {
            400: "SerpApi 拒绝了搜索参数，请调整搜索关键词。",
            401: "SerpApi API Key 无效，请检查 SERPAPI_API_KEY。",
            403: "SerpApi 拒绝访问，请检查密钥和账户权限。",
            402: "SerpApi 账户额度不可用，请检查账户。",
            429: "SerpApi 额度已用尽或请求过于频繁，请检查账户或稍后重试。",
        }
        raise ToolFailed(messages.get(
            response.status_code, "SerpApi 服务暂时无法完成搜索，请稍后重试。",
        ))

    try:
        data = response.json()
    except ValueError:
        raise ToolFailed("SerpApi 返回了无法解析的搜索结果。") from None
    if not isinstance(data, dict):
        raise ToolFailed("SerpApi 返回的搜索结果格式不正确。")
    error = data.get("error")
    # SerpApi can report an empty Google result set as an error message.
    if error == "Google hasn't returned any results for this query.":
        return []
    if error:
        # Do not forward raw provider diagnostics or search_metadata URLs.
        raise ToolFailed("SerpApi 未能完成本次搜索，请检查账户或调整关键词。")
    metadata = data.get("search_metadata", {})
    if isinstance(metadata, dict) and metadata.get("status") not in {None, "Success"}:
        raise ToolFailed("SerpApi 尚未返回成功的搜索结果。")
    items = data.get("organic_results", [])
    if not isinstance(items, list):
        raise ToolFailed("SerpApi 返回的搜索结果列表格式不正确。")

    results: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title, url, snippet = item.get("title"), item.get("link"), item.get("snippet", "")
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        if not title.strip() or not url.startswith(("https://", "http://")):
            continue
        results.append(SearchResult(
            title=title[:500], url=url,
            snippet=snippet[:2000] if isinstance(snippet, str) else "",
        ))
        if len(results) >= max_results:
            break
    return results
