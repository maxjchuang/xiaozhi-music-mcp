#!/usr/bin/env python3
"""Tests for the SearXNG client and MCP-facing search behavior."""

from __future__ import annotations

import json
from io import BytesIO
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

from music_mcp_server import web_search
from web_search import WebSearchError, clear_search_cache, search_searxng


class _Response:
    def __init__(self, payload: object):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self.body


class SearxngClientTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_search_cache()

    @patch("web_search.urlopen")
    def test_search_posts_json_request_and_sanitizes_results(self, urlopen: MagicMock) -> None:
        urlopen.return_value = _Response(
            {
                "results": [
                    {
                        "title": "<b>示例</b> 新闻",
                        "url": "https://example.com/news",
                        "content": "内容 &amp; 摘要",
                        "engines": ["bing", "duckduckgo"],
                        "publishedDate": "2026-09-18",
                    },
                    {"title": "重复", "url": "https://example.com/news", "content": "忽略"},
                    {"title": "危险链接", "url": "javascript:alert(1)", "content": "忽略"},
                ]
            }
        )

        response = search_searxng(
            "今天的新闻",
            max_results=5,
            time_range="day",
            environ={
                "SEARXNG_URL": "http://searxng:8080",
                "WEB_SEARCH_CACHE_TTL": "600",
            },
        )

        self.assertFalse(response.cached)
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].title, "示例 新闻")
        self.assertEqual(response.results[0].snippet, "内容 & 摘要")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://searxng:8080/search")
        form = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(form["format"], ["json"])
        self.assertEqual(form["time_range"], ["day"])

    @patch("web_search.urlopen")
    def test_repeated_search_uses_memory_cache(self, urlopen: MagicMock) -> None:
        urlopen.return_value = _Response(
            {"results": [{"title": "结果", "url": "https://example.com", "content": "摘要"}]}
        )
        environment = {"SEARXNG_URL": "http://localhost:8080", "WEB_SEARCH_CACHE_TTL": "600"}

        first = search_searxng("缓存测试", environ=environment)
        second = search_searxng("缓存测试", environ=environment)

        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(urlopen.call_count, 1)

    @patch("web_search.urlopen")
    def test_json_disabled_has_actionable_error(self, urlopen: MagicMock) -> None:
        urlopen.side_effect = HTTPError("http://localhost/search", 403, "Forbidden", {}, BytesIO())

        with self.assertRaisesRegex(WebSearchError, "启用 json"):
            search_searxng(
                "测试",
                environ={"SEARXNG_URL": "http://localhost:8080", "WEB_SEARCH_CACHE_TTL": "0"},
            )

    def test_rejects_invalid_configuration_and_parameters(self) -> None:
        with self.assertRaisesRegex(WebSearchError, "SEARXNG_URL"):
            search_searxng("测试", environ={"SEARXNG_URL": "file:///tmp/search"})
        with self.assertRaisesRegex(WebSearchError, "time_range"):
            search_searxng("测试", time_range="week")


class WebSearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_returns_structured_results_and_records_analytics(self) -> None:
        recorder = MagicMock()
        fake_response = MagicMock(cached=False)
        fake_result = MagicMock()
        fake_result.public_dict.return_value = {
            "title": "官方消息",
            "url": "https://example.com/official",
            "snippet": "摘要",
        }
        fake_response.results = (fake_result,)
        with (
            patch("music_mcp_server.get_recorder", return_value=recorder),
            patch("music_mcp_server.search_searxng", return_value=fake_response),
        ):
            payload = json.loads(await web_search("最新消息"))

        self.assertTrue(payload["success"])
        self.assertEqual(payload["provider"], "searxng")
        self.assertEqual(payload["result_count"], 1)
        self.assertIn("每句尽量不超过45个汉字", payload["answer_instruction"])
        self.assertIn("每轮不超过300个汉字", payload["answer_instruction"])
        self.assertEqual(
            [call.args[0] for call in recorder.emit.call_args_list],
            ["web_search_started", "web_search_succeeded"],
        )

    async def test_tool_returns_safe_provider_failure(self) -> None:
        with (
            patch("music_mcp_server.record_event", new=AsyncMock()),
            patch("music_mcp_server.search_searxng", side_effect=WebSearchError("无法连接 SearXNG")),
        ):
            payload = json.loads(await web_search("测试"))

        self.assertFalse(payload["success"])
        self.assertEqual(payload["message"], "无法连接 SearXNG")


if __name__ == "__main__":
    unittest.main()
