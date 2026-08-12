#!/usr/bin/env python3
"""Manage local provider processes used by xiaozhi-music-mcp on macOS."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_PREFIX = "com.xiaozhi.music-provider"
USER_ID = os.getuid()
LAUNCH_DOMAIN = f"gui/{USER_ID}"
AUTOSTART_DIR = Path.home() / "Library" / "LaunchAgents"
RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "xiaozhi-music-mcp" / "providers"
LOG_DIR = Path.home() / ".local" / "state" / "xiaozhi" / "logs"
LEGACY_LABELS = {"netease": "com.xiaozhi.netease-api"}


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    display_name: str
    enabled: bool
    endpoint: str
    managed: bool
    working_directory: Path | None
    command: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{SERVICE_PREFIX}.{self.key}"


def load_config(project_root: Path = PROJECT_ROOT) -> dict[str, str]:
    config: dict[str, str] = {}
    for path in (project_root / ".env", project_root / ".env.local"):
        if path.exists():
            config.update(
                {key: value for key, value in dotenv_values(path).items() if value is not None}
            )
    config.update(os.environ)
    return config


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_command(value: str | None, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None or not value.strip():
        return default
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("服务启动命令必须是 JSON 字符串数组") from exc
    if not isinstance(parsed, list) or not parsed or not all(
        isinstance(item, str) and item for item in parsed
    ):
        raise ValueError("服务启动命令必须是非空 JSON 字符串数组")
    return tuple(parsed)


def expand_path(value: str | None, default: str | None = None) -> Path | None:
    selected = value.strip() if value and value.strip() else default
    return Path(selected).expanduser().resolve() if selected else None


def build_specs(config: dict[str, str]) -> list[ProviderSpec]:
    netease_enabled = truthy(config.get("NETEASE_PROVIDER_ENABLED"))
    netease_endpoint = config.get("NETEASE_API_URL", "http://127.0.0.1:3000").strip()
    navidrome_endpoint = config.get("NAVIDROME_URL", "").strip()
    navidrome_enabled = bool(
        navidrome_endpoint
        and config.get("NAVIDROME_USERNAME", "").strip()
        and config.get("NAVIDROME_PASSWORD", "").strip()
    )
    unofficial_enabled = truthy(config.get("UNOFFICIAL_PROVIDER_ENABLED"))
    unofficial_endpoint = config.get("UNOFFICIAL_PROVIDER_URL", "").strip()

    return [
        ProviderSpec(
            key="netease",
            display_name="网易云 API",
            enabled=netease_enabled,
            endpoint=netease_endpoint,
            managed=netease_enabled
            and truthy(config.get("NETEASE_SERVICE_MANAGED"), default=is_local_endpoint(netease_endpoint)),
            working_directory=expand_path(
                config.get("NETEASE_SERVICE_DIR"),
                "~/.local/share/xiaozhi/netease-api-enhanced",
            ),
            command=parse_command(config.get("NETEASE_SERVICE_COMMAND"), ("npm", "start")),
        ),
        ProviderSpec(
            key="navidrome",
            display_name="Navidrome",
            enabled=navidrome_enabled,
            endpoint=navidrome_endpoint,
            managed=navidrome_enabled and truthy(config.get("NAVIDROME_SERVICE_MANAGED")),
            working_directory=expand_path(config.get("NAVIDROME_SERVICE_DIR")),
            command=parse_command(config.get("NAVIDROME_SERVICE_COMMAND")),
        ),
        ProviderSpec(
            key="unofficial",
            display_name="非官方适配器",
            enabled=unofficial_enabled and bool(unofficial_endpoint),
            endpoint=unofficial_endpoint,
            managed=unofficial_enabled and truthy(config.get("UNOFFICIAL_SERVICE_MANAGED")),
            working_directory=expand_path(config.get("UNOFFICIAL_SERVICE_DIR")),
            command=parse_command(config.get("UNOFFICIAL_SERVICE_COMMAND")),
        ),
    ]


def is_local_endpoint(endpoint: str) -> bool:
    hostname = (urlsplit(endpoint).hostname or "").lower()
    return hostname in {"127.0.0.1", "localhost", "::1"}


def endpoint_address(endpoint: str) -> tuple[str, int] | None:
    parsed = urlsplit(endpoint)
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        return None
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def endpoint_ready(endpoint: str, timeout: float = 0.5) -> bool:
    address = endpoint_address(endpoint)
    if not address:
        return False
    try:
        with socket.create_connection(address, timeout=timeout):
            return True
    except OSError:
        return False


def plist_path(spec: ProviderSpec, autostart: bool) -> Path:
    root = AUTOSTART_DIR if autostart else RUNTIME_DIR
    return root / f"{spec.label}.plist"


def remove_legacy_plist(spec: ProviderSpec) -> None:
    legacy_label = LEGACY_LABELS.get(spec.key)
    if not legacy_label:
        return
    path = AUTOSTART_DIR / f"{legacy_label}.plist"
    if path.exists():
        path.unlink()


def render_plist(spec: ProviderSpec, destination: Path) -> None:
    if not spec.working_directory or not spec.working_directory.is_dir():
        raise RuntimeError(f"{spec.display_name} 服务目录不存在：{spec.working_directory}")
    if not spec.command:
        raise RuntimeError(f"{spec.display_name} 未配置 SERVICE_COMMAND")
    destination.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    environment_path = os.environ.get(
        "PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    executable = shutil.which(spec.command[0], path=environment_path)
    if not executable:
        raise RuntimeError(f"找不到启动程序：{spec.command[0]}")
    payload = {
        "Label": spec.label,
        "ProgramArguments": [executable, *spec.command[1:]],
        "WorkingDirectory": str(spec.working_directory),
        "EnvironmentVariables": {"PATH": environment_path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(LOG_DIR / f"{spec.key}.log"),
        "StandardErrorPath": str(LOG_DIR / f"{spec.key}.error.log"),
    }
    temporary = destination.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    os.chmod(temporary, 0o600)
    temporary.replace(destination)


def launch_target(spec: ProviderSpec) -> str:
    return f"{LAUNCH_DOMAIN}/{spec.label}"


def legacy_target(spec: ProviderSpec) -> str | None:
    label = LEGACY_LABELS.get(spec.key)
    return f"{LAUNCH_DOMAIN}/{label}" if label else None


def is_loaded(spec: ProviderSpec) -> bool:
    return subprocess.run(
        ["launchctl", "print", launch_target(spec)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def is_legacy_loaded(spec: ProviderSpec) -> bool:
    target = legacy_target(spec)
    return bool(target) and subprocess.run(
        ["launchctl", "print", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def bootout(spec: ProviderSpec) -> None:
    if is_loaded(spec):
        subprocess.run(
            ["launchctl", "bootout", launch_target(spec)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    target = legacy_target(spec)
    if target and is_legacy_loaded(spec):
        subprocess.run(
            ["launchctl", "bootout", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def wait_until_ready(spec: ProviderSpec, attempts: int = 60) -> bool:
    for _ in range(attempts):
        if endpoint_ready(spec.endpoint):
            return True
        time.sleep(0.25)
    return False


def start(specs: list[ProviderSpec], autostart: bool) -> int:
    result = 0
    for spec in specs:
        if not spec.enabled:
            continue
        if spec.managed:
            try:
                destination = plist_path(spec, autostart)
                if is_loaded(spec) and endpoint_ready(spec.endpoint) and destination.exists():
                    remove_legacy_plist(spec)
                    print(f"Provider {spec.display_name}：已在运行（{spec.endpoint}）")
                    continue
                if is_legacy_loaded(spec) and endpoint_ready(spec.endpoint):
                    print(
                        f"Provider {spec.display_name}：旧版托管服务正在运行；下次重启时自动迁移"
                    )
                    continue
                if endpoint_ready(spec.endpoint) and not is_loaded(spec):
                    print(
                        f"Provider {spec.display_name}：检测到其他进程占用端点，按外部服务使用"
                    )
                    continue
                render_plist(spec, destination)
                bootout(spec)
                subprocess.run(
                    ["launchctl", "bootstrap", LAUNCH_DOMAIN, str(destination)], check=True
                )
                if not wait_until_ready(spec):
                    raise RuntimeError(f"端点未就绪：{spec.endpoint}")
                remove_legacy_plist(spec)
                print(f"Provider {spec.display_name}：已自动启动（{spec.endpoint}）")
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(f"错误：无法启动 Provider {spec.display_name}：{exc}", file=sys.stderr)
                result = 1
        elif endpoint_ready(spec.endpoint):
            print(f"Provider {spec.display_name}：外部服务可用（{spec.endpoint}）")
        else:
            print(
                f"警告：Provider {spec.display_name} 已启用但端点不可用；MCP 将使用后续来源（{spec.endpoint}）",
                file=sys.stderr,
            )
    return result


def stop(specs: list[ProviderSpec]) -> int:
    for spec in specs:
        if is_loaded(spec) or is_legacy_loaded(spec):
            bootout(spec)
            print(f"Provider {spec.display_name}：已停止")
    return 0


def remove_autostart(specs: list[ProviderSpec]) -> int:
    for spec in specs:
        path = plist_path(spec, True)
        if path.exists():
            path.unlink()
        remove_legacy_plist(spec)
    return 0


def status(specs: list[ProviderSpec]) -> int:
    for spec in specs:
        if not spec.enabled:
            print(f"Provider {spec.display_name}：未启用")
        elif spec.managed and (is_loaded(spec) or is_legacy_loaded(spec)):
            state = "运行中" if endpoint_ready(spec.endpoint) else "进程已加载但端点未就绪"
            suffix = "旧版托管，重启后迁移" if is_legacy_loaded(spec) else "托管"
            print(f"Provider {spec.display_name}：{state}（{suffix}）")
        elif endpoint_ready(spec.endpoint):
            print(f"Provider {spec.display_name}：运行中（外部管理）")
        elif spec.managed:
            print(f"Provider {spec.display_name}：已停止（托管）")
        else:
            print(f"Provider {spec.display_name}：端点不可用（外部管理）")
    print("Provider Fangpi：远程 HTTP，无本地进程")
    print("Provider Jamendo：远程 HTTP，无本地进程")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status", "remove-autostart"))
    parser.add_argument("--autostart", action="store_true")
    arguments = parser.parse_args()
    try:
        specs = build_specs(load_config())
    except ValueError as exc:
        print(f"错误：Provider 配置无效：{exc}", file=sys.stderr)
        return 2
    if arguments.command == "start":
        return start(specs, arguments.autostart)
    if arguments.command == "stop":
        return stop(specs)
    if arguments.command == "remove-autostart":
        return remove_autostart(specs)
    return status(specs)


if __name__ == "__main__":
    raise SystemExit(main())
