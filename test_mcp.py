#!/usr/bin/env python3
"""End-to-end smoke test for the stdio MCP server."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_DIR = Path(__file__).resolve().parent
EXPECTED_TOOLS = {"resolve_music_url", "web_search"}


class FakeSearxngHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/search":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps(
            {
                "results": [
                    {
                        "title": "小智联网搜索验收",
                        "url": "https://example.com/acceptance",
                        "content": "本地模拟 SearXNG 返回的验收结果",
                        "engine": "acceptance-test",
                    }
                ]
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def text_from_result(result: object) -> str:
    content = getattr(result, "content", [])
    return "\n".join(
        item.text for item in content if getattr(item, "type", None) == "text"
    )


async def test_mcp_server() -> None:
    searxng = ThreadingHTTPServer(("127.0.0.1", 0), FakeSearxngHandler)
    searxng_thread = threading.Thread(target=searxng.serve_forever, daemon=True)
    searxng_thread.start()
    search_url = f"http://127.0.0.1:{searxng.server_address[1]}"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "music_mcp_server.py")],
        env={
            **os.environ,
            "SEARXNG_URL": search_url,
            "WEB_SEARCH_CACHE_TTL": "0",
            "ANALYTICS_ENABLED": "false",
        },
    )
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "xiaozhi-music-resolver"

                tools = await session.list_tools()
                tool_names = {tool.name for tool in tools.tools}
                assert tool_names == EXPECTED_TOOLS, tool_names
                web_search_tool = next(tool for tool in tools.tools if tool.name == "web_search")
                assert "故事、绘本或图书" in (web_search_tool.description or "")
                assert "必须先使用本工具搜索" in (web_search_tool.description or "")

                resolved = await session.call_tool(
                    "resolve_music_url", {"query": "播放乐鑫官方测试音频"}
                )
                payload = text_from_result(resolved)
                assert '"success": true' in payload
                assert '"device_tool": "self.online_music.play_music"' in payload
                assert '"play_type": "url"' in payload
                assert "https://dl.espressif.com/dl/audio/" in payload

                searched = await session.call_tool(
                    "web_search", {"query": "联网搜索验收", "max_results": 3}
                )
                search_payload = json.loads(text_from_result(searched))
                assert search_payload["success"] is True
                assert search_payload["results"][0]["title"] == "小智联网搜索验收"
                assert search_payload["results"][0]["url"] == "https://example.com/acceptance"

                unsupported = await session.call_tool(
                    "resolve_music_url", {"query": "一首不在白名单里的歌"}
                )
                assert '"success": false' in text_from_result(unsupported)

                invalid = await session.call_tool("resolve_music_url", {"query": ""})
                assert invalid.isError is True
    finally:
        searxng.shutdown()
        searxng.server_close()
        searxng_thread.join(timeout=2)


def main() -> None:
    asyncio.run(test_mcp_server())
    print("✅ 标准 MCP 握手、音乐解析、联网搜索、设备工具交接和参数校验测试通过")


if __name__ == "__main__":
    main()
