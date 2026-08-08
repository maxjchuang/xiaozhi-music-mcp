#!/usr/bin/env python3
"""Integration tests for the opaque LAN audio proxy."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mcp_pipe import AudioProxyServer, REGISTER_PATH


AUDIO = b"ID3-test-audio"


class FakeAudioHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(AUDIO)))
        self.end_headers()
        self.wfile.write(AUDIO)

    def log_message(self, message_format: str, *args: object) -> None:
        pass


class AudioProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeAudioHandler)
        upstream_port = self.upstream.server_address[1]
        self.upstream_url = f"http://127.0.0.1:{upstream_port}/song.mp3"
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()

        self.proxy = AudioProxyServer(("127.0.0.1", 0), "http://placeholder", "test-secret")
        proxy_port = self.proxy.server_address[1]
        self.proxy.public_base_url = f"http://127.0.0.1:{proxy_port}"
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()

    def tearDown(self) -> None:
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def test_register_and_stream_without_exposing_upstream(self) -> None:
        body = json.dumps({"url": self.upstream_url, "title": "测试歌曲"}).encode()
        register = Request(
            self.proxy.public_base_url + REGISTER_PATH,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        )
        with urlopen(register) as response:
            public_url = json.load(response)["url"]

        self.assertNotIn(self.upstream_url, public_url)
        with urlopen(public_url) as response:
            self.assertEqual(response.read(), AUDIO)
            self.assertEqual(response.headers.get_content_type(), "audio/mpeg")

    def test_registration_requires_local_secret(self) -> None:
        body = json.dumps({"url": self.upstream_url}).encode()
        register = Request(self.proxy.public_base_url + REGISTER_PATH, data=body, method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(register)
        self.assertEqual(raised.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
