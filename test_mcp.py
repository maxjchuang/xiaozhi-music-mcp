#!/usr/bin/env python3
"""End-to-end smoke test for the stdio MCP server."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_DIR = Path(__file__).resolve().parent
EXPECTED_TOOLS = {"resolve_music_url"}


def text_from_result(result: object) -> str:
    content = getattr(result, "content", [])
    return "\n".join(
        item.text for item in content if getattr(item, "type", None) == "text"
    )


async def test_mcp_server() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "music_mcp_server.py")],
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "xiaozhi-music-resolver"

            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert tool_names == EXPECTED_TOOLS, tool_names

            resolved = await session.call_tool(
                "resolve_music_url", {"query": "播放乐鑫官方测试音频"}
            )
            payload = text_from_result(resolved)
            assert '"success": true' in payload
            assert '"device_tool": "self.online_music.play_music"' in payload
            assert '"play_type": "url"' in payload
            assert "https://dl.espressif.com/dl/audio/" in payload

            unsupported = await session.call_tool(
                "resolve_music_url", {"query": "一首不在白名单里的歌"}
            )
            assert '"success": false' in text_from_result(unsupported)

            invalid = await session.call_tool("resolve_music_url", {"query": ""})
            assert invalid.isError is True


def main() -> None:
    asyncio.run(test_mcp_server())
    print("✅ 标准 MCP 握手、URL 解析、设备工具交接和参数校验测试通过")


if __name__ == "__main__":
    main()
