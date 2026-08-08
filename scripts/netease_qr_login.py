#!/usr/bin/env python3
"""Poll a local NetEase QR login and store MUSIC_U without printing it."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


STATUS_MESSAGES = {
    800: "二维码已过期",
    801: "等待扫码",
    802: "已扫码，等待手机确认",
    803: "登录成功",
}


def request_status(base_url: str, key: str) -> dict[str, object]:
    query = urlencode({"key": key, "timestamp": int(time.time() * 1000)})
    with urlopen(f"{base_url.rstrip('/')}/login/qr/check?{query}", timeout=20) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("登录接口返回了无效数据")
    return payload


def store_music_u(config_path: Path, cookie: str) -> None:
    match = re.search(r"(?:^|;\s*)MUSIC_U=([^;]+)", cookie)
    if not match:
        raise RuntimeError("登录成功，但响应中没有 MUSIC_U")
    setting = f"NETEASE_COOKIE=MUSIC_U={match.group(1)}"
    current = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = [line for line in current.splitlines() if not line.startswith("NETEASE_COOKIE=")]
    lines.append(setting)
    content = "\n".join(lines) + "\n"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".netease-env-", dir=config_path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary_name, config_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".local/share/xiaozhi/netease-api-enhanced/.env",
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    previous_code: int | None = None
    while time.monotonic() < deadline:
        try:
            payload = request_status(args.base_url, args.key)
        except (TimeoutError, URLError, OSError):
            time.sleep(2)
            continue
        code = int(payload.get("code", 0))
        if code != previous_code:
            print(STATUS_MESSAGES.get(code, f"登录状态：{code}"), flush=True)
            previous_code = code
        if code == 803:
            store_music_u(args.config, str(payload.get("cookie", "")))
            print("账号凭据已安全保存到本机服务配置", flush=True)
            return 0
        if code == 800:
            return 2
        time.sleep(2)
    print("等待登录确认超时", flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
