#!/usr/bin/env python3
"""Bridge a local stdio MCP server to a Xiaozhi WebSocket endpoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError
import websockets

from usage_analytics import get_recorder
from feishu_sync import start_sync_worker


LOGGER = logging.getLogger("xiaozhi-mcp-pipe")
INITIAL_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 60
REGISTER_PATH = "/_register"
STREAM_PREFIX = "/stream/"
MEDIA_PREFIX = "/media/"
MAX_REGISTER_BODY = 96 * 1024
MAX_REGISTERED_STREAMS = 256
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096
MAX_LYRICS_BYTES = 64 * 1024


@dataclass(slots=True)
class StreamSource:
    url: str
    content_type: str
    title: str
    expires_at: float
    artist: str = ""
    album: str = ""
    duration_ms: int | None = None
    artwork_url: str = ""
    lyrics: str = ""
    lyrics_url: str = ""
    provider: str = ""
    trace_id: str = ""
    playback_reported: bool = False
    playback_lock: threading.Lock = field(default_factory=threading.Lock)
    background_jpeg: bytes | None = None
    disc_jpeg: bytes | None = None
    assets_lock: threading.Lock = field(default_factory=threading.Lock)


class AudioProxyServer(ThreadingHTTPServer):
    """Threading server carrying an in-memory, short-lived stream registry."""

    def __init__(self, address: tuple[str, int], public_base_url: str, register_token: str):
        super().__init__(address, AudioProxyHandler)
        self.public_base_url = public_base_url.rstrip("/")
        self.register_token = register_token
        self.stream_ttl = max(60, int(os.getenv("MUSIC_PROXY_STREAM_TTL", "1800")))
        self.streams: dict[str, StreamSource] = {}
        self.streams_lock = threading.Lock()

    def register(
        self,
        url: str,
        content_type: str,
        title: str,
        *,
        artist: str = "",
        album: str = "",
        duration_ms: int | None = None,
        artwork_url: str = "",
        lyrics: str = "",
        lyrics_url: str = "",
        provider: str = "",
        trace_id: str = "",
    ) -> tuple[str, str]:
        now = time.time()
        token = secrets.token_urlsafe(18)
        with self.streams_lock:
            expired = [key for key, value in self.streams.items() if value.expires_at <= now]
            for key in expired:
                self.streams.pop(key, None)
            if len(self.streams) >= MAX_REGISTERED_STREAMS:
                oldest = min(self.streams, key=lambda key: self.streams[key].expires_at)
                self.streams.pop(oldest, None)
            self.streams[token] = StreamSource(
                url=url,
                content_type=content_type,
                title=title,
                expires_at=now + self.stream_ttl,
                artist=artist,
                album=album,
                duration_ms=duration_ms,
                artwork_url=artwork_url,
                lyrics=lyrics.encode("utf-8")[:MAX_LYRICS_BYTES].decode("utf-8", errors="ignore"),
                lyrics_url=lyrics_url,
                provider=provider,
                trace_id=trace_id,
            )
        return (
            f"{self.public_base_url}{MEDIA_PREFIX}{token}/audio",
            f"{self.public_base_url}{MEDIA_PREFIX}{token}/manifest.json",
        )

    def resolve(self, token: str) -> StreamSource | None:
        with self.streams_lock:
            source = self.streams.get(token)
            if source is not None and source.expires_at <= time.time():
                self.streams.pop(token, None)
                return None
            return source

    def media_url(self, token: str, name: str) -> str:
        return f"{self.public_base_url}{MEDIA_PREFIX}{token}/{name}"


def upstream_ssl_context() -> ssl.SSLContext:
    """Build a verified context compatible with the Espressif CDN chain."""
    context = ssl.create_default_context()
    # Python 3.13 enables X509 strict mode by default. The Espressif CDN chain
    # is trusted by the system but lacks an Authority Key Identifier on one
    # certificate; retain CA and hostname verification while relaxing only
    # that additional structural check.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


class AudioProxyHandler(BaseHTTPRequestHandler):
    """Register upstreams locally and expose opaque stream URLs to the LAN."""

    protocol_version = "HTTP/1.1"

    @property
    def proxy_server(self) -> AudioProxyServer:
        return self.server  # type: ignore[return-value]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != REGISTER_PATH or self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_error(404)
            return

        expected = f"Bearer {self.proxy_server.register_token}"
        if not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
            self.send_error(401)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "invalid content length")
            return
        if content_length <= 0 or content_length > MAX_REGISTER_BODY:
            self.send_error(413)
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400, "invalid json")
            return
        url = str(payload.get("url", "")).strip()
        if not _valid_upstream_url(url):
            self.send_error(400, "invalid upstream url")
            return
        content_type = str(payload.get("content_type", "audio/mpeg"))[:100]
        title = str(payload.get("title", ""))[:200]
        artwork_url = str(payload.get("artwork_url", "")).strip()
        lyrics_url = str(payload.get("lyrics_url", "")).strip()
        if artwork_url and not _valid_upstream_url(artwork_url):
            artwork_url = ""
        if lyrics_url and not _valid_upstream_url(lyrics_url):
            lyrics_url = ""
        duration_ms = payload.get("duration_ms")
        if not isinstance(duration_ms, int) or duration_ms < 0:
            duration_ms = None
        public_url, metadata_url = self.proxy_server.register(
            url,
            content_type,
            title,
            artist=str(payload.get("artist", ""))[:200],
            album=str(payload.get("album", ""))[:200],
            duration_ms=duration_ms,
            artwork_url=artwork_url,
            lyrics=str(payload.get("lyrics", "")),
            lyrics_url=lyrics_url,
            provider=str(payload.get("provider", ""))[:100],
            trace_id=str(payload.get("trace_id", ""))[:128],
        )
        body = json.dumps({"url": public_url, "metadata_url": metadata_url}).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path.startswith(STREAM_PREFIX):
            token = path.removeprefix(STREAM_PREFIX)
            self._serve_audio(token)
            return
        if not path.startswith(MEDIA_PREFIX):
            self.send_error(404)
            return
        parts = path.removeprefix(MEDIA_PREFIX).split("/", 1)
        if len(parts) != 2:
            self.send_error(404)
            return
        token, resource = parts
        source = self.proxy_server.resolve(token)
        if source is None:
            self.send_error(404, "stream expired or unknown")
            return

        if resource == "audio":
            self._report_playback_started(source)
            self._proxy_upstream(source.url, source.content_type, source.title, allow_range=True)
        elif resource == "manifest.json":
            self._serve_manifest(token, source)
        elif resource == "lyrics.lrc":
            self._serve_lyrics(source)
        elif resource in {"background.jpg", "disc.jpg"}:
            self._serve_artwork(source, background=resource == "background.jpg")
        else:
            self.send_error(404)

    def _serve_audio(self, token: str) -> None:
        source = self.proxy_server.resolve(token)
        if source is None:
            self.send_error(404, "stream expired or unknown")
            return
        self._report_playback_started(source)
        self._proxy_upstream(source.url, source.content_type, source.title, allow_range=True)

    def _report_playback_started(self, source: StreamSource) -> None:
        with source.playback_lock:
            if source.playback_reported:
                return
            source.playback_reported = True
        recorder = get_recorder()
        if recorder is not None:
            recorder.emit(
                "playback_started",
                source="proxy",
                trace_id=source.trace_id,
                payload={
                    "title": source.title,
                    "artist": source.artist,
                    "album": source.album,
                    "provider": source.provider,
                    "duration_ms": source.duration_ms,
                },
            )

    def _serve_manifest(self, token: str, source: StreamSource) -> None:
        artwork = None
        if source.artwork_url:
            self._ensure_artwork(source)
            artwork = {
                "background_url": self.proxy_server.media_url(token, "background.jpg"),
                "disc_url": self.proxy_server.media_url(token, "disc.jpg"),
            }
        lyrics = None
        if source.lyrics or source.lyrics_url:
            lyrics = {
                "url": self.proxy_server.media_url(token, "lyrics.lrc"),
                "format": "lrc",
                "offset_ms": 0,
            }
        body = json.dumps(
            {
                "schema_version": 1,
                "title": source.title,
                "artist": source.artist,
                "album": source.album,
                "duration_ms": source.duration_ms,
                "artwork": artwork,
                "lyrics": lyrics,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8")

    def _serve_lyrics(self, source: StreamSource) -> None:
        if source.lyrics:
            body = source.lyrics.encode("utf-8")
        elif source.lyrics_url:
            try:
                body, _ = _download_limited(source.lyrics_url, MAX_LYRICS_BYTES, "text/plain")
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                LOGGER.warning("歌词上游不可用（%s）：%s", source.title, exc)
                self.send_error(502, "lyrics upstream unavailable")
                return
        else:
            self.send_error(404, "lyrics unavailable")
            return
        self._send_bytes(body, "text/plain; charset=utf-8")

    def _serve_artwork(self, source: StreamSource, *, background: bool) -> None:
        if not source.artwork_url:
            self.send_error(404, "artwork unavailable")
            return
        attribute = "background_jpeg" if background else "disc_jpeg"
        if not self._ensure_artwork(source):
            self.send_error(502, "artwork unavailable")
            return
        body = getattr(source, attribute)
        self._send_bytes(body, "image/jpeg", cache=True)

    def _ensure_artwork(self, source: StreamSource) -> bool:
        with source.assets_lock:
            try:
                if source.background_jpeg is None or source.disc_jpeg is None:
                    original, _ = _download_limited(source.artwork_url, MAX_IMAGE_BYTES, "image/*")
                    background_jpeg, disc_jpeg = _prepare_artwork(original)
                    source.background_jpeg = background_jpeg
                    source.disc_jpeg = disc_jpeg
                return True
            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                ValueError,
                UnidentifiedImageError,
                Image.DecompressionBombError,
            ) as exc:
                LOGGER.warning("封面处理失败（%s）：%s", source.title, exc)
                return False

    def _send_bytes(self, body: bytes, content_type: str, *, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "private, max-age=1800")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_upstream(self, url: str, content_type: str, title: str, *, allow_range: bool) -> None:

        headers = {"User-Agent": "xiaozhi-music-mcp/1.0", "Accept": "audio/mpeg"}
        if allow_range and (range_header := self.headers.get("Range")):
            headers["Range"] = range_header
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30, context=upstream_ssl_context()) as upstream:
                self.send_response(upstream.status)
                for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if value := upstream.headers.get(name):
                        self.send_header(name, value)
                if not upstream.headers.get("Content-Type"):
                    self.send_header("Content-Type", content_type)
                self.send_header("Connection", "close")
                self.end_headers()
                while chunk := upstream.read(64 * 1024):
                    self.wfile.write(chunk)
        except HTTPError as exc:
            LOGGER.warning("音频上游返回 HTTP %s（%s）", exc.code, title)
            self.send_error(502, "upstream HTTP error")
        except (URLError, TimeoutError, OSError) as exc:
            LOGGER.warning("音频代理失败（%s）：%s", title, exc)
            try:
                self.send_error(502, "upstream unavailable")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, message_format: str, *args: object) -> None:
        LOGGER.info("[audio-proxy] %s", message_format % args)


def _valid_upstream_url(url: str) -> bool:
    parts = urlsplit(url)
    return bool(parts.scheme in {"http", "https"} and parts.netloc and not parts.username and not parts.password)


def _download_limited(url: str, limit: int, accept: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "xiaozhi-music-mcp/2.1", "Accept": accept})
    with urlopen(request, timeout=10, context=upstream_ssl_context()) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > limit:
            raise ValueError("upstream resource is too large")
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("upstream resource is too large")
        return body, response.headers.get("Content-Type", "")


def _jpeg_bytes(image: Image.Image, quality: int = 92) -> bytes:
    output = BytesIO()
    image.convert("RGB").save(
        output,
        format="JPEG",
        quality=quality,
        subsampling=0,
        optimize=True,
    )
    return output.getvalue()


def _dither_for_rgb565(image: Image.Image) -> Image.Image:
    """Apply subtle ordered dithering before the device quantizes to RGB565."""
    rgb = image.convert("RGB")
    pixels = rgb.load()
    # Centred Bayer 4x4 offsets. A small amplitude breaks up broad gradient
    # bands without creating visible grain on the 360 px display.
    bayer = (
        (0, 8, 2, 10),
        (12, 4, 14, 6),
        (3, 11, 1, 9),
        (15, 7, 13, 5),
    )
    for y in range(rgb.height):
        for x in range(rgb.width):
            red, green, blue = pixels[x, y]
            offset = (bayer[y & 3][x & 3] - 7.5) * 0.55
            pixels[x, y] = (
                max(0, min(255, round(red + offset))),
                max(0, min(255, round(green + offset * 0.55))),
                max(0, min(255, round(blue + offset))),
            )
    return rgb


def _prepare_artwork(body: bytes) -> tuple[bytes, bytes]:
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(BytesIO(body)) as decoded:
        decoded.load()
        if decoded.width * decoded.height > MAX_IMAGE_PIXELS:
            raise ValueError("artwork dimensions are too large")
        rgb = ImageOps.exif_transpose(decoded).convert("RGB")
        background = ImageOps.fit(rgb, (360, 360), method=Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(radius=4))
        background = ImageEnhance.Color(background).enhance(0.88)
        background = ImageEnhance.Brightness(background).enhance(0.52)
        background = _dither_for_rgb565(background)
        disc = ImageOps.fit(rgb, (192, 192), method=Image.Resampling.LANCZOS)
        disc = disc.filter(ImageFilter.UnsharpMask(radius=0.8, percent=65, threshold=3))
        return _jpeg_bytes(background, 94), _jpeg_bytes(disc, 95)


def discover_lan_ip() -> str:
    """Discover the IPv4 address used for outbound LAN traffic."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.168.31.1", 9))
        return str(probe.getsockname()[0])


def start_audio_proxy() -> AudioProxyServer:
    port = int(os.getenv("MUSIC_PROXY_PORT", "8765"))
    lan_ip = discover_lan_ip()
    public_base_url = f"http://{lan_ip}:{port}"
    register_token = secrets.token_urlsafe(32)
    server = AudioProxyServer(("0.0.0.0", port), public_base_url, register_token)
    server.daemon_threads = True
    os.environ["MUSIC_PROXY_REGISTER_URL"] = f"http://127.0.0.1:{port}{REGISTER_PATH}"
    os.environ["MUSIC_PROXY_REGISTER_TOKEN"] = register_token
    thread = threading.Thread(target=server.serve_forever, name="audio-proxy", daemon=True)
    thread.start()
    LOGGER.info("动态音乐局域网代理已启动：%s%s<临时令牌>/audio", public_base_url, MEDIA_PREFIX)
    return server


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
    load_dotenv(Path(__file__).with_name(".env.local"), override=False)
    log_dir = Path(__file__).with_name("logs")
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=(
            logging.StreamHandler(),
            RotatingFileHandler(
                log_dir / "mcp_pipe.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            ),
        ),
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
        proxy = start_audio_proxy()
    except (OSError, ValueError) as exc:
        LOGGER.error("无法启动测试音频代理：%s", exc)
        return 2

    sync_worker = start_sync_worker()

    try:
        asyncio.run(run_forever(endpoint, server_script))
    except KeyboardInterrupt:
        LOGGER.info("已停止 MCP 桥接")
    finally:
        if sync_worker is not None:
            sync_worker.stop()
        proxy.shutdown()
        proxy.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
