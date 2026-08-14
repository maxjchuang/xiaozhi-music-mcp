#!/usr/bin/env python3
"""Tests for the Feishu CLI process and authentication adapter."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from lark_cli import LarkCli, LarkCliAuth, LarkCliError, REQUIRED_BASE_SCOPES


class FakeCli:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[list[str]] = []

    def run(self, arguments, *, timeout=60):
        self.calls.append(list(arguments))
        return self.payload

    def run_interactive(self, arguments):
        self.calls.append(list(arguments))


class LarkCliTests(unittest.TestCase):
    def test_json_success_envelope_is_returned(self) -> None:
        completed = subprocess.CompletedProcess(
            ["lark-cli"], 0, stdout=json.dumps({"ok": True, "data": {"value": 1}}), stderr=""
        )
        with patch("lark_cli.subprocess.run", return_value=completed):
            result = LarkCli("/usr/local/bin/lark-cli").run(["base", "+table-list"])
        self.assertEqual(result["data"]["value"], 1)

    def test_error_envelope_raises(self) -> None:
        completed = subprocess.CompletedProcess(
            ["lark-cli"], 1, stdout="", stderr=json.dumps({"ok": False, "error": {"message": "AUTH_REQUIRED"}})
        )
        with patch("lark_cli.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(LarkCliError, "AUTH_REQUIRED"):
                LarkCli("/usr/local/bin/lark-cli").run(["auth", "status"])

    def test_auth_status_requires_verified_user_and_base_scopes(self) -> None:
        payload = {
            "verified": True,
            "identities": {
                "user": {
                    "verified": True,
                    "status": "ready",
                    "tokenStatus": "valid",
                    "userName": "测试用户",
                    "scope": " ".join(sorted(REQUIRED_BASE_SCOPES)),
                }
            },
        }
        status = LarkCliAuth(FakeCli(payload)).status()  # type: ignore[arg-type]
        self.assertTrue(status["ready"])
        self.assertEqual(status["user_name"], "测试用户")

    def test_auth_status_reports_missing_scopes(self) -> None:
        payload = {
            "verified": True,
            "identities": {"user": {"verified": True, "status": "ready", "tokenStatus": "valid", "scope": "offline_access"}},
        }
        status = LarkCliAuth(FakeCli(payload)).status()  # type: ignore[arg-type]
        self.assertFalse(status["ready"])
        self.assertIn("base:record:create", status["missing_scopes"])


if __name__ == "__main__":
    unittest.main()
