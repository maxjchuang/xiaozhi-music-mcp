#!/bin/bash
set -euo pipefail

SERVICE_LABEL="com.xiaozhi.music-mcp"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
TEMPLATE_PATH="${PROJECT_ROOT}/config/${SERVICE_LABEL}.plist.template"
LOG_DIR="${PROJECT_ROOT}/logs"
USER_ID="$(id -u)"
LAUNCH_DOMAIN="gui/${USER_ID}"
SERVICE_TARGET="${LAUNCH_DOMAIN}/${SERVICE_LABEL}"
USER_LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
AUTOSTART_PLIST="${USER_LAUNCH_AGENTS}/${SERVICE_LABEL}.plist"
RUNTIME_DIR="${HOME}/Library/Application Support/xiaozhi-music-mcp"
RUNTIME_PLIST="${RUNTIME_DIR}/${SERVICE_LABEL}.plist"
PROVIDER_MANAGER="${PROJECT_ROOT}/scripts/provider_manager.py"
PROVIDER_LOG_DIR="${HOME}/.local/state/xiaozhi/logs"

usage() {
    cat <<'EOF'
用法：bash scripts/music_service.sh <命令>

命令：
  install             交互选择是否启用登录自启动，并启动服务
  start               启动所需 Provider 与 MCP；退出终端后仍继续运行
  stop                停止 MCP 与本项目托管的 Provider
  restart             重启 Provider 与 MCP
  status              查看 MCP、Provider 和登录自启动状态
  enable-autostart    开启登录自启动，并立即启动服务
  disable-autostart   关闭登录自启动；若服务正在运行则保持运行
  logs                持续查看服务日志，按 Ctrl+C 退出
EOF
}

validate_installation() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        echo "错误：服务管理脚本目前仅支持 macOS。" >&2
        exit 1
    fi
    if [[ ! -x "${PYTHON_BIN}" ]]; then
        echo "错误：找不到虚拟环境 Python：${PYTHON_BIN}" >&2
        echo "请先按照 README 创建 .venv 并安装依赖。" >&2
        exit 1
    fi
    if [[ ! -f "${PROJECT_ROOT}/mcp_pipe.py" || ! -f "${TEMPLATE_PATH}" ||
          ! -f "${PROVIDER_MANAGER}" ]]; then
        echo "错误：项目文件不完整。" >&2
        exit 1
    fi
    if [[ ! -f "${PROJECT_ROOT}/.env.local" && ! -f "${PROJECT_ROOT}/.env" ]]; then
        echo "错误：未找到 .env.local 或 .env，请先配置 MCP_ENDPOINT。" >&2
        exit 1
    fi
}

start_providers() {
    local arguments=(start)
    if is_autostart_enabled; then
        arguments+=(--autostart)
    fi
    "${PYTHON_BIN}" "${PROVIDER_MANAGER}" "${arguments[@]}"
}

stop_providers() {
    "${PYTHON_BIN}" "${PROVIDER_MANAGER}" stop
}

remove_provider_autostart() {
    "${PYTHON_BIN}" "${PROVIDER_MANAGER}" remove-autostart
}

show_provider_status() {
    "${PYTHON_BIN}" "${PROVIDER_MANAGER}" status
}

escape_sed_replacement() {
    printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

render_plist() {
    local destination="$1"
    local escaped_project_root
    local escaped_python_bin
    local escaped_log_dir
    escaped_project_root="$(escape_sed_replacement "${PROJECT_ROOT}")"
    escaped_python_bin="$(escape_sed_replacement "${PYTHON_BIN}")"
    escaped_log_dir="$(escape_sed_replacement "${LOG_DIR}")"

    mkdir -p "$(dirname "${destination}")" "${LOG_DIR}"
    sed \
        -e "s|__PROJECT_ROOT__|${escaped_project_root}|g" \
        -e "s|__PYTHON_BIN__|${escaped_python_bin}|g" \
        -e "s|__LOG_DIR__|${escaped_log_dir}|g" \
        "${TEMPLATE_PATH}" > "${destination}"
    chmod 600 "${destination}"
    plutil -lint "${destination}" >/dev/null
}

is_loaded() {
    launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1
}

is_running() {
    launchctl print "${SERVICE_TARGET}" 2>/dev/null | grep -q 'state = running'
}

is_autostart_enabled() {
    [[ -f "${AUTOSTART_PLIST}" ]]
}

bootout_if_loaded() {
    if is_loaded; then
        launchctl bootout "${SERVICE_TARGET}"
    fi
}

bootstrap_service() {
    local plist_path="$1"
    launchctl bootstrap "${LAUNCH_DOMAIN}" "${plist_path}"
    for _ in {1..40}; do
        if is_running && lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.25
    done
    echo "错误：服务未就绪，请查看 ${LOG_DIR}/launchagent.error.log" >&2
    return 1
}

start_service() {
    validate_installation
    start_providers
    if is_running; then
        echo "小智音乐 MCP 已在运行。"
        return 0
    fi
    if is_loaded; then
        bootout_if_loaded
    fi

    local plist_path
    if is_autostart_enabled; then
        plist_path="${AUTOSTART_PLIST}"
    else
        plist_path="${RUNTIME_PLIST}"
    fi
    render_plist "${plist_path}"
    bootstrap_service "${plist_path}"
    echo "小智音乐 MCP 已启动；现在可以关闭终端或退出 Codex。"
}

stop_service() {
    if is_loaded; then
        launchctl bootout "${SERVICE_TARGET}"
        echo "小智音乐 MCP 已停止。"
    else
        echo "小智音乐 MCP 当前未运行。"
    fi
    stop_providers
}

enable_autostart() {
    validate_installation
    bootout_if_loaded
    stop_providers
    render_plist "${AUTOSTART_PLIST}"
    "${PYTHON_BIN}" "${PROVIDER_MANAGER}" start --autostart
    bootstrap_service "${AUTOSTART_PLIST}"
    echo "登录自启动已开启，服务已运行。"
}

disable_autostart() {
    local was_running="false"
    if is_running; then
        was_running="true"
    fi
    bootout_if_loaded
    stop_providers
    if [[ -f "${AUTOSTART_PLIST}" ]]; then
        rm "${AUTOSTART_PLIST}"
    fi
    remove_provider_autostart
    echo "登录自启动已关闭。"

    if [[ "${was_running}" == "true" ]]; then
        validate_installation
        start_providers
        render_plist "${RUNTIME_PLIST}"
        bootstrap_service "${RUNTIME_PLIST}"
        echo "当前服务保持运行；下次登录不会自动启动。"
    fi
}

show_status() {
    if is_running; then
        local process_id
        process_id="$(launchctl print "${SERVICE_TARGET}" | awk '/pid =/ {print $3; exit}')"
        echo "运行状态：运行中（PID ${process_id:-未知}）"
    elif is_loaded; then
        echo "运行状态：已加载但进程未运行"
    else
        echo "运行状态：已停止"
    fi

    if is_autostart_enabled; then
        echo "登录自启动：已开启"
    else
        echo "登录自启动：已关闭"
    fi

    if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
        echo "音频代理：正在监听 TCP 8765"
    else
        echo "音频代理：未监听 TCP 8765"
    fi
    show_provider_status
}

install_interactive() {
    validate_installation
    local answer=""
    if [[ -t 0 ]]; then
        read -r -p "是否启用登录自动启动？[y/N] " answer
    fi
    case "${answer}" in
        y|Y|yes|YES|Yes)
            enable_autostart
            ;;
        *)
            disable_autostart
            start_service
            ;;
    esac
}

command_name="${1:-}"
case "${command_name}" in
    install)
        install_interactive
        ;;
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        start_service
        ;;
    status)
        show_status
        ;;
    enable-autostart)
        enable_autostart
        ;;
    disable-autostart)
        disable_autostart
        ;;
    logs)
        mkdir -p "${LOG_DIR}" "${PROVIDER_LOG_DIR}"
        touch "${LOG_DIR}/launchagent.log" "${LOG_DIR}/launchagent.error.log"
        touch "${PROVIDER_LOG_DIR}/netease.log" "${PROVIDER_LOG_DIR}/netease.error.log" \
              "${PROVIDER_LOG_DIR}/navidrome.log" "${PROVIDER_LOG_DIR}/navidrome.error.log" \
              "${PROVIDER_LOG_DIR}/unofficial.log" "${PROVIDER_LOG_DIR}/unofficial.error.log"
        tail -f "${LOG_DIR}/launchagent.log" "${LOG_DIR}/launchagent.error.log" \
            "${PROVIDER_LOG_DIR}/netease.log" "${PROVIDER_LOG_DIR}/netease.error.log" \
            "${PROVIDER_LOG_DIR}/navidrome.log" "${PROVIDER_LOG_DIR}/navidrome.error.log" \
            "${PROVIDER_LOG_DIR}/unofficial.log" "${PROVIDER_LOG_DIR}/unofficial.error.log"
        ;;
    -h|--help|help|"")
        usage
        ;;
    *)
        echo "错误：未知命令 ${command_name}" >&2
        usage >&2
        exit 2
        ;;
esac
