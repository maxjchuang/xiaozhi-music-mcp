#!/usr/bin/env python3
"""Local integration test for the WebSocket-to-stdio bridge."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import websockets

from mcp_pipe import bridge_once, redact_endpoint, redact_message, validate_endpoint


PROJECT_DIR = Path(__file__).resolve().parent


async def test_bridge() -> None:
    completed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def fake_xiaozhi(websocket: object) -> None:
        try:
            await websocket.send(  # type: ignore[attr-defined]
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "fake-xiaozhi", "version": "1.0"},
                        },
                    }
                )
            )
            initialized = json.loads(await websocket.recv())  # type: ignore[attr-defined]
            assert initialized["id"] == 1
            assert initialized["result"]["serverInfo"]["name"] == "xiaozhi-music-resolver"

            await websocket.send(  # type: ignore[attr-defined]
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            )
            await websocket.send(  # type: ignore[attr-defined]
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            )
            tools = json.loads(await websocket.recv())  # type: ignore[attr-defined]
            assert [tool["name"] for tool in tools["result"]["tools"]] == [
                "resolve_music_url"
            ]
            completed.set_result(None)
        except Exception as exc:
            if not completed.done():
                completed.set_exception(exc)
            raise

    async with websockets.serve(fake_xiaozhi, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        endpoint = f"ws://127.0.0.1:{port}/mcp?token=local-test-secret"
        assert validate_endpoint(endpoint) == endpoint
        assert "local-test-secret" not in redact_endpoint(endpoint)
        assert "local-test-secret" not in redact_message(f"连接 {endpoint} 失败")

        bridge = asyncio.create_task(
            bridge_once(endpoint, PROJECT_DIR / "music_mcp_server.py")
        )
        await asyncio.wait_for(completed, timeout=10)
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)


def main() -> None:
    asyncio.run(test_bridge())
    print("✅ WebSocket ↔ stdio 双向桥接、Token 脱敏和工具发现测试通过")


if __name__ == "__main__":
    main()
