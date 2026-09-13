#!/usr/bin/env python3
"""Manage Feishu authorization and the local analytics queue."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from feishu_sync import FeishuApiError, FeishuBaseClient, FeishuSyncWorker  # noqa: E402
from lark_cli import LarkCliAuth, LarkCliError  # noqa: E402
from usage_analytics import AnalyticsRecorder, AnalyticsStore, default_database_path  # noqa: E402


def load_environment() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(PROJECT_ROOT / ".env.local", override=False)


def feishu_enabled() -> bool:
    return os.getenv("FEISHU_ANALYTICS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def auth_required_on_start() -> bool:
    return os.getenv("FEISHU_AUTH_REQUIRED_ON_START", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def auth_status() -> int:
    if not feishu_enabled():
        print("飞书统计：未启用")
        return 0
    try:
        status = LarkCliAuth().status()
    except LarkCliError as exc:
        print(f"飞书统计：配置错误（{exc}）", file=sys.stderr)
        return 2
    if not status["ready"]:
        missing = ", ".join(status["missing_scopes"])
        print("飞书统计：AUTH_REQUIRED")
        if missing:
            print(f"缺少权限：{missing}", file=sys.stderr)
        return 3
    print(f"飞书统计：已登录（{status['user_name'] or status['open_id']}），Token {status['token_status']}")
    return 0


def auth_login() -> int:
    if not feishu_enabled():
        print("飞书统计尚未启用，请先设置 FEISHU_ANALYTICS_ENABLED=true。", file=sys.stderr)
        return 2
    try:
        print("正在启动飞书 CLI Device Flow；请按终端提示完成授权……")
        status = LarkCliAuth().login()
    except (LarkCliError, OSError) as exc:
        print(f"飞书登录失败：{exc}", file=sys.stderr)
        return 3
    print(f"飞书登录成功：{status['user_name'] or status['open_id']}")
    return 0


def auth_preflight(interactive: bool) -> int:
    result = auth_status()
    if result in {2, 3} and interactive:
        result = auth_login()
    if result != 0 and not auth_required_on_start():
        print("警告：飞书统计暂不可用；音乐服务将继续启动，事件会保存在本地。", file=sys.stderr)
        return 0
    return result


def auth_logout() -> int:
    try:
        LarkCliAuth().logout()
    except LarkCliError as exc:
        print(f"飞书退出失败：{exc}", file=sys.stderr)
        return 3
    print("已通过飞书 CLI 清除本机用户登录态。")
    return 0


def analytics_status() -> int:
    status = AnalyticsStore(default_database_path()).status()
    print(f"本地事件：{status['events']}")
    print(f"待同步：{status['pending']}，发送中：{status['sending']}，已同步：{status['synced']}，死信：{status['dead']}")
    return 0


def telemetry_status() -> int:
    status = AnalyticsStore(default_database_path()).playback_status()
    print(f"播放记录：{status['playbacks']}")
    print(f"会话记录：{status['sessions']}")
    print(
        f"投影待同步：{status['pending']}，已同步：{status['synced']}，死信：{status['dead']}"
    )
    return 0


def analytics_rebuild() -> int:
    count = AnalyticsStore(default_database_path()).rebuild_playback_projections()
    print(f"已从原始事件重建 {count} 条播放记录。")
    return 0


def inspect_playback(playback_id: str) -> int:
    playback = AnalyticsStore(default_database_path()).get_playback(playback_id)
    if playback is None:
        print(f"未找到播放记录：{playback_id}", file=sys.stderr)
        return 4
    print(json.dumps(playback, ensure_ascii=False, indent=2))
    return 0


def inspect_session(session_id: str) -> int:
    session = AnalyticsStore(default_database_path()).get_session(session_id)
    if session is None:
        print(f"未找到会话记录：{session_id}", file=sys.stderr)
        return 4
    print(json.dumps(session, ensure_ascii=False, indent=2))
    return 0


def query_sessions(device_id: str, since: str, until: str, limit: int) -> int:
    rows = AnalyticsStore(default_database_path()).query_sessions(
        device_id=device_id, since=since, until=until, limit=limit
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def delete_session(session_id: str, confirmed: bool) -> int:
    if not confirmed:
        print("删除会话会清除本地原始事件和播放投影；确认后请追加 --yes。", file=sys.stderr)
        return 2
    deleted = AnalyticsStore(default_database_path()).delete_session(session_id)
    print(
        f"已删除会话 {session_id}：{deleted['events']} 条原始事件，"
        f"{deleted['playbacks']} 条播放记录，{deleted['sessions']} 条会话记录。"
    )
    return 0


def analytics_cleanup(confirmed: bool) -> int:
    if not confirmed:
        print("保留期清理只删除已同步数据；确认后请追加 --yes。", file=sys.stderr)
        return 2
    try:
        raw_days = int(os.getenv("ANALYTICS_RAW_RETENTION_DAYS", "30"))
        projection_days = int(os.getenv("ANALYTICS_PROJECTION_RETENTION_DAYS", "180"))
    except ValueError:
        print("保留期必须是整数天。", file=sys.stderr)
        return 2
    deleted = AnalyticsStore(default_database_path()).cleanup_retention(
        raw_days=raw_days, projection_days=projection_days
    )
    print(
        f"保留期清理完成：{deleted['events']} 条原始事件，"
        f"{deleted['playbacks']} 条播放记录，{deleted['sessions']} 条会话记录。"
    )
    return 0


def analytics_retry() -> int:
    count = AnalyticsStore(default_database_path()).retry_dead()
    print(f"已重新投递 {count} 条死信事件。")
    return 0


def analytics_sync() -> int:
    try:
        worker = FeishuSyncWorker(
            AnalyticsStore(default_database_path()), FeishuBaseClient.from_env()
        )
        count = 0
        while synced := worker.sync_once():
            count += synced
    except (LarkCliError, FeishuApiError, OSError, ValueError) as exc:
        print(f"同步失败：{exc}", file=sys.stderr)
        return 3
    print(f"已同步 {count} 条事件。")
    return 0


def analytics_test() -> int:
    store = AnalyticsStore(default_database_path())
    event_id = AnalyticsRecorder(store).emit(
        "analytics_test",
        source="manager",
        payload={"message": "小智飞书统计端到端测试"},
    )
    try:
        worker = FeishuSyncWorker(store, FeishuBaseClient.from_env())
        count = 0
        while store.event_sync_status(event_id) != "synced":
            synced = worker.sync_once()
            if synced == 0:
                break
            count += synced
    except (LarkCliError, FeishuApiError, OSError, ValueError) as exc:
        print(f"测试事件 {event_id} 已保存在本地，但同步失败：{exc}", file=sys.stderr)
        return 3
    status = store.event_sync_status(event_id)
    if status != "synced":
        print(f"测试失败：事件 {event_id} 当前状态为 {status}。", file=sys.stderr)
        return 3
    print(f"测试成功：事件 {event_id} 已写入飞书，共同步 {count} 条。")
    return 0


def set_project_env(values: dict[str, str]) -> None:
    path = PROJECT_ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    updated: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else ""
        if key in remaining:
            updated.append(f"{key}={remaining.pop(key)}")
        else:
            updated.append(line)
    updated.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    path.chmod(0o600)


def analytics_init(*, create_base: bool = False, base_name: str = "小智使用分析") -> int:
    base_token = os.getenv("FEISHU_BASE_TOKEN", "").strip()
    created_base_url = ""
    try:
        if base_token:
            client = FeishuBaseClient.from_env()
            table_id, session_table_id, play_table_id, dashboard_id = client.initialize()
        else:
            should_create = create_base
            if not should_create:
                if not sys.stdin.isatty():
                    print(
                        "初始化失败：缺少 FEISHU_BASE_TOKEN。非交互模式下可添加 "
                        "--create-base 自动创建多维表格。",
                        file=sys.stderr,
                    )
                    return 3
                answer = input(
                    f"尚未配置 FEISHU_BASE_TOKEN，是否自动创建并初始化多维表格“{base_name}”？[Y/n] "
                ).strip().lower()
                should_create = answer in {"", "y", "yes", "是"}
            if not should_create:
                print("已取消创建。请配置 FEISHU_BASE_TOKEN 后重新执行 analytics init。")
                return 2
            client, default_table_id, created_base_url = FeishuBaseClient.create_base(base_name)
            table_id, session_table_id, play_table_id, dashboard_id = client.initialize(
                fresh_base=True, default_table_id=default_table_id
            )
    except (LarkCliError, FeishuApiError, OSError, ValueError) as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 3
    set_project_env(
        {
            "FEISHU_ANALYTICS_ENABLED": "true",
            "LARK_CLI_BIN": str(LarkCliAuth().cli.executable),
            "FEISHU_BASE_TOKEN": client.base_token,
            "FEISHU_EVENT_TABLE_ID": table_id,
            "FEISHU_SESSION_TABLE_ID": session_table_id,
            "FEISHU_PLAY_TABLE_ID": play_table_id,
            "FEISHU_DASHBOARD_ID": dashboard_id,
        }
    )
    print(f"原始事件表：{table_id}")
    print(f"会话记录表：{session_table_id}")
    print(f"音乐播放记录表：{play_table_id}")
    print(f"统计仪表盘：{dashboard_id}")
    if created_base_url:
        print(f"多维表格：{created_base_url}")
    print("初始化成功，配置已写入 .env。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_subparsers(dest="group", required=True)
    auth = group.add_parser("auth")
    auth_commands = auth.add_subparsers(dest="command", required=True)
    auth_commands.add_parser("status")
    auth_commands.add_parser("login")
    auth_commands.add_parser("logout")
    preflight = auth_commands.add_parser("preflight")
    preflight.add_argument("--interactive", action="store_true")
    analytics = group.add_parser("analytics")
    analytics_commands = analytics.add_subparsers(dest="command", required=True)
    analytics_commands.add_parser("status")
    initialize = analytics_commands.add_parser("init")
    initialize.add_argument("--create-base", action="store_true")
    initialize.add_argument("--base-name", default="小智使用分析")
    analytics_commands.add_parser("retry")
    analytics_commands.add_parser("sync")
    analytics_commands.add_parser("test")
    analytics_commands.add_parser("telemetry-status")
    analytics_commands.add_parser("rebuild")
    inspect = analytics_commands.add_parser("inspect-playback")
    inspect.add_argument("playback_id")
    inspect_session_parser = analytics_commands.add_parser("inspect-session")
    inspect_session_parser.add_argument("session_id")
    query_session_parser = analytics_commands.add_parser("query-sessions")
    query_session_parser.add_argument("--device-id", default="")
    query_session_parser.add_argument("--since", default="")
    query_session_parser.add_argument("--until", default="")
    query_session_parser.add_argument("--limit", type=int, default=100)
    delete_session_parser = analytics_commands.add_parser("delete-session")
    delete_session_parser.add_argument("session_id")
    delete_session_parser.add_argument("--yes", action="store_true")
    cleanup_parser = analytics_commands.add_parser("cleanup")
    cleanup_parser.add_argument("--yes", action="store_true")
    return parser


def main() -> int:
    load_environment()
    arguments = build_parser().parse_args()
    if arguments.group == "auth":
        if arguments.command == "status":
            return auth_status()
        if arguments.command == "login":
            return auth_login()
        if arguments.command == "logout":
            return auth_logout()
        if arguments.command == "preflight":
            return auth_preflight(arguments.interactive)
    if arguments.group == "analytics":
        if arguments.command == "init":
            return analytics_init(
                create_base=arguments.create_base,
                base_name=arguments.base_name,
            )
        if arguments.command == "status":
            return analytics_status()
        if arguments.command == "retry":
            return analytics_retry()
        if arguments.command == "sync":
            return analytics_sync()
        if arguments.command == "test":
            return analytics_test()
        if arguments.command == "telemetry-status":
            return telemetry_status()
        if arguments.command == "rebuild":
            return analytics_rebuild()
        if arguments.command == "inspect-playback":
            return inspect_playback(arguments.playback_id)
        if arguments.command == "inspect-session":
            return inspect_session(arguments.session_id)
        if arguments.command == "query-sessions":
            return query_sessions(
                arguments.device_id, arguments.since, arguments.until, arguments.limit
            )
        if arguments.command == "delete-session":
            return delete_session(arguments.session_id, arguments.yes)
        if arguments.command == "cleanup":
            return analytics_cleanup(arguments.yes)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
