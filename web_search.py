#!/usr/bin/env python3
"""Small, dependency-free SearXNG client used by the MCP search tool."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import json
import os
import re
import threading
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_SEARXNG_URL = "http://127.0.0.1:8080"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CACHE_TTL_SECONDS = 600
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
VALID_TIME_RANGES = frozenset({"", "day", "month", "year"})
_TAG_PATTERN = re.compile(r"<[^>]+>")
_SPACE_PATTERN = re.compile(r"\s+")


class WebSearchError(RuntimeError):
    """A safe, user-facing failure raised by the configured search provider."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str = ""
    published_at: str = ""

    def public_dict(self) -> dict[str, str]:
        payload = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
        }
        if self.source:
            payload["source"] = self.source
        if self.published_at:
            payload["published_at"] = self.published_at
        return payload


@dataclass(frozen=True, slots=True)
class SearchResponse:
    results: tuple[SearchResult, ...]
    cached: bool = False


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    results: tuple[SearchResult, ...]


_CACHE: dict[tuple[str, str, str, int, int, str], _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


def _bounded_number(raw_value: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _search_url(configured_url: str) -> str:
    value = configured_url.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise WebSearchError("SEARXNG_URL 必须是有效的 http:// 或 https:// 地址")
    path = parts.path.rstrip("/")
    if not path.endswith("/search"):
        path = f"{path}/search"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _plain_text(value: Any, limit: int) -> str:
    text = unescape(_TAG_PATTERN.sub(" ", str(value or "")))
    return _SPACE_PATTERN.sub(" ", text).strip()[:limit]


def _public_http_url(value: Any) -> str:
    url = str(value or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        return ""
    return url


def _parse_results(payload: Mapping[str, Any], limit: int) -> tuple[SearchResult, ...]:
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        raise WebSearchError("SearXNG 返回了无效的 results 字段")

    parsed: list[SearchResult] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        url = _public_http_url(item.get("url"))
        if not url or url in seen_urls:
            continue
        title = _plain_text(item.get("title"), 300)
        if not title:
            continue
        engines = item.get("engines")
        if isinstance(engines, list):
            source = ", ".join(_plain_text(engine, 50) for engine in engines[:3] if engine)
        else:
            source = _plain_text(item.get("engine"), 100)
        parsed.append(
            SearchResult(
                title=title,
                url=url,
                snippet=_plain_text(item.get("content"), 1000),
                source=source,
                published_at=_plain_text(item.get("publishedDate") or item.get("pubdate"), 100),
            )
        )
        seen_urls.add(url)
        if len(parsed) >= limit:
            break
    return tuple(parsed)


def clear_search_cache() -> None:
    """Clear process-local search entries, mainly for configuration reloads and tests."""
    with _CACHE_LOCK:
        _CACHE.clear()


def search_searxng(
    query: str,
    *,
    max_results: int = 5,
    language: str = "zh-CN",
    time_range: str = "",
    environ: Mapping[str, str] | None = None,
) -> SearchResponse:
    """Query SearXNG's JSON API and return sanitized, deduplicated results."""
    source = environ or os.environ
    normalized_query = _SPACE_PATTERN.sub(" ", query).strip()
    if not normalized_query:
        raise WebSearchError("搜索关键词不能为空")
    if len(normalized_query) > 500:
        raise WebSearchError("搜索关键词不能超过 500 个字符")
    if not 1 <= max_results <= 10:
        raise WebSearchError("搜索结果数量必须在 1 到 10 之间")
    selected_time_range = time_range.strip().lower()
    if selected_time_range not in VALID_TIME_RANGES:
        raise WebSearchError("time_range 只支持 day、month、year 或留空")
    selected_language = language.strip() or "zh-CN"
    if len(selected_language) > 20:
        raise WebSearchError("language 参数过长")

    endpoint = _search_url(source.get("SEARXNG_URL", DEFAULT_SEARXNG_URL))
    safe_search = int(_bounded_number(source.get("WEB_SEARCH_SAFESEARCH", "1"), 1, 0, 2))
    cache_ttl = int(
        _bounded_number(source.get("WEB_SEARCH_CACHE_TTL", "600"), DEFAULT_CACHE_TTL_SECONDS, 0, 86400)
    )
    cache_key = (normalized_query.casefold(), selected_language, selected_time_range, max_results, safe_search, endpoint)
    now = time.monotonic()
    if cache_ttl:
        with _CACHE_LOCK:
            entry = _CACHE.get(cache_key)
            if entry and entry.expires_at > now:
                return SearchResponse(entry.results, cached=True)
            if entry:
                _CACHE.pop(cache_key, None)

    form = {
        "q": normalized_query,
        "format": "json",
        "language": selected_language,
        "safesearch": str(safe_search),
        "categories": "general",
    }
    if selected_time_range:
        form["time_range"] = selected_time_range
    body = urlencode(form).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "xiaozhi-music-mcp/1.0",
        },
    )
    timeout = _bounded_number(
        source.get("WEB_SEARCH_TIMEOUT_SECONDS", "10"), DEFAULT_TIMEOUT_SECONDS, 1, 30
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code == 403:
            detail = "SearXNG 拒绝了 JSON 请求，请在 settings.yml 的 search.formats 中启用 json"
        else:
            detail = f"SearXNG 请求失败（HTTP {exc.code}）"
        raise WebSearchError(detail) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise WebSearchError("无法连接 SearXNG，请检查服务地址和运行状态") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise WebSearchError("SearXNG 响应过大，已停止处理")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebSearchError("SearXNG 没有返回有效的 JSON") from exc
    if not isinstance(payload, Mapping):
        raise WebSearchError("SearXNG 返回格式无效")

    results = _parse_results(payload, max_results)
    if cache_ttl:
        with _CACHE_LOCK:
            if len(_CACHE) >= 256:
                oldest_key = min(_CACHE, key=lambda key: _CACHE[key].expires_at)
                _CACHE.pop(oldest_key, None)
            _CACHE[cache_key] = _CacheEntry(now + cache_ttl, results)
    return SearchResponse(results)
