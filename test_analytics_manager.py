#!/usr/bin/env python3
"""Tests for analytics initialization choices."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts import analytics_manager


class AnalyticsManagerTests(unittest.TestCase):
    def test_init_without_token_is_actionable_in_noninteractive_mode(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("scripts.analytics_manager.sys.stdin.isatty", return_value=False),
            patch("scripts.analytics_manager.FeishuBaseClient.create_base") as create_base,
        ):
            result = analytics_manager.analytics_init()

        self.assertEqual(result, 3)
        create_base.assert_not_called()

    def test_init_prompts_and_creates_base_when_user_accepts(self) -> None:
        client = MagicMock()
        client.base_token = "base-created"
        client.initialize.return_value = ("tbl-events", "dbs-dashboard")
        auth = MagicMock()
        auth.cli.executable = "/usr/local/bin/lark-cli"
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("scripts.analytics_manager.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="") as prompt,
            patch(
                "scripts.analytics_manager.FeishuBaseClient.create_base",
                return_value=(client, "tbl-default", "https://example/base/base-created"),
            ) as create_base,
            patch("scripts.analytics_manager.LarkCliAuth", return_value=auth),
            patch("scripts.analytics_manager.set_project_env") as set_env,
        ):
            result = analytics_manager.analytics_init()

        self.assertEqual(result, 0)
        prompt.assert_called_once()
        create_base.assert_called_once_with("小智使用分析")
        client.initialize.assert_called_once_with(
            fresh_base=True, default_table_id="tbl-default"
        )
        self.assertEqual(set_env.call_args.args[0]["FEISHU_BASE_TOKEN"], "base-created")

    def test_init_create_base_flag_skips_prompt(self) -> None:
        client = MagicMock(base_token="base-created")
        client.initialize.return_value = ("tbl-events", "dbs-dashboard")
        auth = MagicMock()
        auth.cli.executable = "/usr/local/bin/lark-cli"
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("builtins.input") as prompt,
            patch(
                "scripts.analytics_manager.FeishuBaseClient.create_base",
                return_value=(client, "tbl-default", ""),
            ),
            patch("scripts.analytics_manager.LarkCliAuth", return_value=auth),
            patch("scripts.analytics_manager.set_project_env"),
        ):
            result = analytics_manager.analytics_init(create_base=True, base_name="家庭小智分析")

        self.assertEqual(result, 0)
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
