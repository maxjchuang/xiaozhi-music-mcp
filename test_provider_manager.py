#!/usr/bin/env python3

from __future__ import annotations

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from provider_manager import (
    ProviderSpec,
    build_specs,
    endpoint_address,
    is_local_endpoint,
    parse_command,
    start,
)


class ProviderManagerTests(unittest.TestCase):
    def test_netease_is_managed_by_default_for_local_endpoint(self) -> None:
        specs = build_specs(
            {"NETEASE_PROVIDER_ENABLED": "true", "NETEASE_API_URL": "http://127.0.0.1:3000"}
        )
        netease = specs[0]
        self.assertTrue(netease.enabled)
        self.assertTrue(netease.managed)
        self.assertEqual(netease.command, ("npm", "start"))

    def test_remote_netease_is_external_by_default(self) -> None:
        specs = build_specs(
            {"NETEASE_PROVIDER_ENABLED": "true", "NETEASE_API_URL": "https://music.example.test"}
        )
        self.assertFalse(specs[0].managed)

    def test_custom_command_requires_json_array(self) -> None:
        self.assertEqual(parse_command('["node", "server.js"]'), ("node", "server.js"))
        with self.assertRaises(ValueError):
            parse_command("node server.js")

    def test_endpoint_helpers_support_ipv4_and_https_defaults(self) -> None:
        self.assertTrue(is_local_endpoint("http://localhost:3000/search"))
        self.assertEqual(endpoint_address("https://example.test/search"), ("example.test", 443))

    def test_managed_provider_failure_does_not_block_mcp_startup(self) -> None:
        provider = ProviderSpec(
            key="navidrome",
            display_name="Navidrome",
            enabled=True,
            endpoint="http://127.0.0.1:4533",
            managed=True,
            working_directory=Path("/missing/navidrome"),
            command=("navidrome",),
        )

        with (
            patch("provider_manager.is_loaded", return_value=False),
            patch("provider_manager.is_legacy_loaded", return_value=False),
            patch("provider_manager.endpoint_ready", return_value=False),
            patch("provider_manager.render_plist", side_effect=RuntimeError("服务目录不存在")),
            patch("sys.stderr") as stderr,
        ):
            result = start([provider], autostart=False)

        self.assertEqual(result, 0)
        output = "".join(call.args[0] for call in stderr.write.call_args_list if call.args)
        self.assertIn("不会阻止 MCP 启动", output)
        self.assertIn("Navidrome", output)


if __name__ == "__main__":
    unittest.main()
