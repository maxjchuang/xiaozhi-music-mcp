#!/usr/bin/env python3
"""Bridge a local stdio MCP server to a Xiaozhi WebSocket endpoint."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
import websockets


LOGGER = logging.getLogger("xiaozhi-mcp-pipe")
INITIAL_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 60


def redact_endpoint(endpoint: str) -> str:
    """Return an endpoint safe to include in logs."""
    parts = urlsplit(endpoint)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "token=***" if parts.query else "", ""))


def redact_message(message: str) -> str:
    """Hide token query values that may appear in dependency errors."""
    return re.sub(r"(?i)(token=)[^&\s]+", r"\1***", message)


def validate_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme not in {"ws", "wss"} or not parts.netloc:
        raise ValueError("MCP_ENDPOINT 必须是有效的 ws:// 或 wss:// 地址")
    return endpoint


async def websocket_to_process(websocket: object, process: asyncio.subprocess.Process) -> None:
    assert process.stdin is not None
    async for message in websocket:  # type: ignore[attr-defined]
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        process.stdin.write(message.rstrip("\r\n").encode("utf-8") + b"\n")
        await process.stdin.drain()


async def process_to_websocket(process: asyncio.subprocess.Process, websocket: object) -> None:
    assert process.stdout is not None
    while line := await process.stdout.readline():
        await websocket.send(line.decode("utf-8").rstrip("\r\n"))  # type: ignore[attr-defined]
    raise RuntimeError("本地 MCP 服务已退出")


async def log_process_stderr(process: asyncio.subprocess.Process) -> None:
    assert process.stderr is not None
    while line := await process.stderr.readline():
        LOGGER.info("[mcp-server] %s", line.decode("utf-8", errors="replace").rstrip())


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def bridge_once(endpoint: str, server_script: Path) -> None:
    LOGGER.info("连接小智 MCP 接入点：%s", redact_endpoint(endpoint))
    # Passing an explicit context avoids a Python 3.13 + websockets default
    # context incompatibility seen with the Xiaozhi certificate chain while
    # preserving normal hostname and CA verification.
    ssl_context = ssl.create_default_context() if urlsplit(endpoint).scheme == "wss" else None
    async with websockets.connect(
        endpoint,
        ssl=ssl_context,
        # websockets 15+ auto-discovers system proxies. Some HTTPS inspection
        # proxies present a chain rejected by Python 3.13, while Xiaozhi is
        # directly reachable; match the official bridge's direct connection.
        proxy=None,
        ping_interval=20,
        ping_timeout=20,
    ) as websocket:
        LOGGER.info("小智 MCP 接入点连接成功")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(server_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        LOGGER.info("已启动本地 MCP 服务：%s", server_script)
        tasks = {
            asyncio.create_task(websocket_to_process(websocket, process)),
            asyncio.create_task(process_to_websocket(process, websocket)),
            asyncio.create_task(log_process_stderr(process)),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
            raise RuntimeError("MCP 桥接任务意外结束")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await terminate_process(process)


async def run_forever(endpoint: str, server_script: Path) -> None:
    backoff = INITIAL_BACKOFF_SECONDS
    while True:
        try:
            await bridge_once(endpoint, server_script)
            backoff = INITIAL_BACKOFF_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("连接中断：%s；%s 秒后重试", redact_message(str(exc)), backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    endpoint = os.getenv("MCP_ENDPOINT", "").strip()
    if not endpoint:
        LOGGER.error("请先设置 MCP_ENDPOINT；可参考 .env.example")
        return 2
    try:
        validate_endpoint(endpoint)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    default_script = Path(__file__).with_name("music_mcp_server.py")
    server_script = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else default_script
    server_script = server_script.resolve()
    if not server_script.is_file():
        LOGGER.error("找不到本地 MCP 服务：%s", server_script)
        return 2

    try:
        asyncio.run(run_forever(endpoint, server_script))
    except KeyboardInterrupt:
        LOGGER.info("已停止 MCP 桥接")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
