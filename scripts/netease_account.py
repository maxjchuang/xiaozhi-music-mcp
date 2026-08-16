#!/usr/bin/env python3
"""Manage the NetEase account used by the local enhanced API service."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from netease_qr_login import poll_login, remove_music_u
from provider_manager import build_specs, load_config


def request_json(base_url: str, path: str, parameters: dict[str, object] | None = None) -> dict:
    query = dict(parameters or {})
    query["timestamp"] = int(time.time() * 1000)
    url = f"{base_url.rstrip('/')}{path}?{urlencode(query)}"
    try:
        with urlopen(url, timeout=20) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"无法访问网易云 API：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("网易云 API 返回了无效数据")
    return payload


def account_identity(payload: dict) -> tuple[str, str] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    user_id = profile.get("userId") or account.get("id")
    if not user_id:
        return None
    nickname = profile.get("nickname") or account.get("userName") or ""
    if str(nickname).startswith("1000_"):
        nickname = ""
    return str(user_id), str(nickname)


def has_local_cookie(config_path: Path | None) -> bool:
    if not config_path or not config_path.exists():
        return False
    return any(
        line.startswith("NETEASE_COOKIE=") and line.partition("=")[2].strip()
        for line in config_path.read_text(encoding="utf-8").splitlines()
    )


def show_status(base_url: str, config_path: Path | None = None) -> int:
    try:
        identity = account_identity(request_json(base_url, "/login/status"))
    except RuntimeError as exc:
        local_state = "；本地 Cookie 已配置" if has_local_cookie(config_path) else ""
        print(f"网易云登录状态：无法联网检查{local_state}（{exc}）")
        return 1
    if not identity:
        local_state = "（本地 Cookie 已配置，可能已失效）" if has_local_cookie(config_path) else ""
        print(f"网易云登录状态：未登录或登录已失效{local_state}")
        return 0
    user_id, nickname = identity
    account_label = f"{nickname}，ID {user_id}" if nickname else f"ID {user_id}，API 未返回昵称"
    print(f"网易云登录状态：已登录（{account_label}）")
    return 0


def create_qr_login(base_url: str, config_path: Path, timeout: int) -> int:
    key_payload = request_json(base_url, "/login/qr/key")
    data = key_payload.get("data")
    key = data.get("unikey") if isinstance(data, dict) else None
    if not key:
        raise RuntimeError("网易云 API 未返回二维码 Key")

    qr_payload = request_json(base_url, "/login/qr/create", {"key": key, "qrimg": "true"})
    qr_data = qr_payload.get("data")
    qr_image = qr_data.get("qrimg") if isinstance(qr_data, dict) else None
    qr_url = qr_data.get("qrurl") if isinstance(qr_data, dict) else None
    if not isinstance(qr_image, str) or "," not in qr_image:
        raise RuntimeError("网易云 API 未返回可用的二维码")

    try:
        image_bytes = base64.b64decode(qr_image.split(",", 1)[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("网易云二维码图片解码失败") from exc

    qr_path = Path.home() / ".local" / "state" / "xiaozhi" / "netease-login.png"
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    qr_path.write_bytes(image_bytes)
    qr_path.chmod(0o600)
    completed = subprocess.run(["open", str(qr_path)], check=False)
    print(f"二维码已保存到：{qr_path}")
    if completed.returncode != 0:
        print("无法自动打开二维码，请手动打开上述文件。")
    if qr_url:
        print(f"登录链接：{qr_url}")
    print("请使用网易云音乐 App 扫码并在手机上确认。")
    try:
        return poll_login(base_url, str(key), config_path, timeout)
    finally:
        qr_path.unlink(missing_ok=True)


def logout(base_url: str, config_path: Path) -> int:
    try:
        request_json(base_url, "/logout")
    except RuntimeError as exc:
        print(f"警告：服务端退出请求失败（{exc}），继续清除本地凭据。")
    existed = remove_music_u(config_path)
    print("已清除本机网易云登录凭据。" if existed else "本机没有已保存的网易云登录凭据。")
    return 0


def resolve_settings(command: str) -> tuple[str, Path | None]:
    spec = build_specs(load_config())[0]
    if not spec.enabled:
        raise RuntimeError("网易云 Provider 未启用，请先设置 NETEASE_PROVIDER_ENABLED=true")
    if command == "status":
        config_path = spec.working_directory / ".env" if spec.managed and spec.working_directory else None
        return spec.endpoint, config_path
    if not spec.managed:
        raise RuntimeError("当前是外部网易云 API，请在 API 部署机器上管理登录账号")
    if not spec.working_directory:
        raise RuntimeError("未配置 NETEASE_SERVICE_DIR")
    return spec.endpoint, spec.working_directory / ".env"


def main() -> int:
    parser = argparse.ArgumentParser(description="网易云音乐账号管理")
    parser.add_argument("command", choices=("status", "login", "logout", "relogin"))
    parser.add_argument("--timeout", type=int, default=180)
    arguments = parser.parse_args()
    try:
        base_url, config_path = resolve_settings(arguments.command)
        if arguments.command == "status":
            return show_status(base_url, config_path)
        assert config_path is not None
        if arguments.command == "logout":
            return logout(base_url, config_path)
        if arguments.command == "relogin":
            logout(base_url, config_path)
        return create_qr_login(base_url, config_path, arguments.timeout)
    except RuntimeError as exc:
        print(f"错误：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
