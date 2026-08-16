#!/usr/bin/env python3

from __future__ import annotations

import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from netease_account import account_identity, has_local_cookie
from netease_qr_login import remove_music_u, store_music_u


class NeteaseAccountTests(unittest.TestCase):
    def test_account_identity_supports_account_without_profile(self) -> None:
        identity = account_identity(
            {"data": {"account": {"id": 12345, "userName": "测试账号"}, "profile": None}}
        )
        self.assertEqual(identity, ("12345", "测试账号"))

    def test_account_identity_hides_internal_username(self) -> None:
        identity = account_identity(
            {"data": {"account": {"id": 12345, "userName": "1000_internal-token"}}}
        )
        self.assertEqual(identity, ("12345", ""))

    def test_store_and_remove_cookie_preserve_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".env"
            config_path.write_text("HOST=127.0.0.1\nNETEASE_COOKIE=MUSIC_U=old\n", encoding="utf-8")

            store_music_u(config_path, "MUSIC_U=new-value; Path=/")
            stored = config_path.read_text(encoding="utf-8")
            self.assertIn("HOST=127.0.0.1", stored)
            self.assertIn("NETEASE_COOKIE=MUSIC_U=new-value", stored)
            self.assertNotIn("MUSIC_U=old", stored)

            self.assertTrue(remove_music_u(config_path))
            cleared = config_path.read_text(encoding="utf-8")
            self.assertIn("HOST=127.0.0.1", cleared)
            self.assertNotIn("NETEASE_COOKIE", cleared)

    def test_local_cookie_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".env"
            config_path.write_text("NETEASE_COOKIE=MUSIC_U=value\n", encoding="utf-8")
            self.assertTrue(has_local_cookie(config_path))
            config_path.write_text("NETEASE_COOKIE=\n", encoding="utf-8")
            self.assertFalse(has_local_cookie(config_path))


if __name__ == "__main__":
    unittest.main()
