#!/usr/bin/env python3
"""Process adapter for deterministic Feishu CLI authentication and API calls."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Mapping, Sequence


class LarkCliError(RuntimeError):
    pass


class LarkCli:
    def __init__(self, executable: str | None = None):
        configured = executable or os.getenv("LARK_CLI_BIN", "").strip()
        self.executable = configured or shutil.which("lark-cli") or ""
        if not self.executable:
            raise LarkCliError("找不到 lark-cli，请先安装飞书 CLI")

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        executable_directory = os.path.dirname(os.path.abspath(self.executable))
        path_entries = environment.get("PATH", "").split(os.pathsep)
        if executable_directory not in path_entries:
            environment["PATH"] = os.pathsep.join(
                [executable_directory, *(entry for entry in path_entries if entry)]
            )
        environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        return environment

    def run(self, arguments: Sequence[str], *, timeout: float = 60) -> dict[str, Any]:
        environment = self._environment()
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LarkCliError(f"无法执行 lark-cli：{exc}") from exc
        output = completed.stdout.strip() or completed.stderr.strip()
        try:
            payload = json.loads(output) if output else {}
        except json.JSONDecodeError as exc:
            message = output[-1000:] if output else f"退出码 {completed.returncode}"
            raise LarkCliError(f"lark-cli 返回了非 JSON 结果：{message}") from exc
        if completed.returncode != 0:
            raise LarkCliError(self._error_message(payload, completed.returncode))
        if payload.get("ok") is False:
            raise LarkCliError(self._error_message(payload, completed.returncode))
        if "code" in payload and int(payload.get("code", -1)) != 0:
            raise LarkCliError(str(payload.get("msg", payload.get("code"))))
        return payload

    def run_interactive(self, arguments: Sequence[str]) -> None:
        environment = self._environment()
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                env=environment,
                check=False,
            )
        except OSError as exc:
            raise LarkCliError(f"无法执行 lark-cli：{exc}") from exc
        if completed.returncode != 0:
            raise LarkCliError(f"lark-cli 登录失败，退出码 {completed.returncode}")

    @staticmethod
    def _error_message(payload: Mapping[str, Any], returncode: int) -> str:
        error = payload.get("error", {})
        if isinstance(error, Mapping):
            return str(error.get("message") or error.get("hint") or error.get("code") or f"退出码 {returncode}")
        return str(payload.get("message") or payload.get("msg") or f"退出码 {returncode}")


REQUIRED_BASE_SCOPES = frozenset(
    {
        "offline_access",
        "base:app:create",
        "base:record:create",
        "base:record:read",
        "base:table:create",
        "base:table:read",
        "base:table:update",
        "base:field:create",
        "base:field:read",
        "base:field:update",
        "base:dashboard:create",
        "base:dashboard:read",
    }
)


class LarkCliAuth:
    def __init__(self, cli: LarkCli | None = None):
        self.cli = cli or LarkCli()

    def status(self) -> dict[str, Any]:
        payload = self.cli.run(["auth", "status", "--verify"], timeout=30)
        user = payload.get("identities", {}).get("user", {})
        scopes = set(str(user.get("scope", "")).split())
        missing_scopes = sorted(REQUIRED_BASE_SCOPES - scopes)
        ready = bool(
            payload.get("verified")
            and user.get("verified")
            and user.get("status") == "ready"
            and user.get("tokenStatus") == "valid"
            and not missing_scopes
        )
        return {
            "ready": ready,
            "user_name": str(user.get("userName", "")),
            "open_id": str(user.get("openId", "")),
            "token_status": str(user.get("tokenStatus", "unknown")),
            "expires_at": str(user.get("expiresAt", "")),
            "missing_scopes": missing_scopes,
        }

    def login(self) -> dict[str, Any]:
        try:
            self.cli.run(["config", "show"], timeout=10)
        except LarkCliError:
            self.cli.run_interactive(["config", "init", "--new"])
        self.cli.run_interactive(["auth", "login", "--domain", "base"])
        status = self.status()
        if not status["ready"]:
            missing = ", ".join(status["missing_scopes"])
            raise LarkCliError(f"飞书登录完成但权限仍不完整：{missing or status['token_status']}")
        return status

    def logout(self) -> None:
        self.cli.run(["auth", "logout"], timeout=30)
