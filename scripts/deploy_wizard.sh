#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
ENV_FILE="${PROJECT_ROOT}/.env"

usage() {
    cat <<'EOF'
用法：bash scripts/deploy_wizard.sh [--skip-tests]

交互完成 Python 环境、依赖、本地配置、测试和后台服务安装。
已有 .env 和 .env.local 会被保留；敏感配置不会输出到终端。

选项：
  --skip-tests  跳过本地测试
  -h, --help    显示帮助
EOF
}

info() {
    printf '\n==> %s\n' "$1"
}

fail() {
    printf '错误：%s\n' "$1" >&2
    exit 1
}

ask_yes_no() {
    local prompt="$1"
    local default_answer="$2"
    local answer=""
    local suffix="[y/N]"
    if [[ "${default_answer}" == "yes" ]]; then
        suffix="[Y/n]"
    fi
    read -r -p "${prompt} ${suffix} " answer
    if [[ -z "${answer}" ]]; then
        [[ "${default_answer}" == "yes" ]]
        return
    fi
    [[ "${answer}" =~ ^[Yy]([Ee][Ss])?$ ]]
}

env_value() {
    local key="$1"
    local file
    for file in "${ENV_FILE}" "${PROJECT_ROOT}/.env.local"; do
        if [[ -f "${file}" ]]; then
            local value
            value="$(awk -F= -v wanted="${key}" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${file}")"
            if [[ -n "${value}" ]]; then
                printf '%s' "${value}"
                return 0
            fi
        fi
    done
    return 1
}

set_env_value() {
    local key="$1"
    local value="$2"
    CONFIG_VALUE="${value}" "${VENV_PYTHON}" - "${ENV_FILE}" "${key}" <<'PY'
from pathlib import Path
import os
import sys
import tempfile

path = Path(sys.argv[1])
key = sys.argv[2]
value = os.environ["CONFIG_VALUE"]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
replacement = f"{key}={value}"
updated = []
replaced = False
for line in lines:
    if line.startswith(f"{key}="):
        if not replaced:
            updated.append(replacement)
            replaced = True
    else:
        updated.append(line)
if not replaced:
    updated.append(replacement)

path.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(prefix=".env-", dir=path.parent)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(updated) + "\n")
    os.replace(temporary_name, path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
}

skip_tests="false"
case "${1:-}" in
    "") ;;
    --skip-tests) skip_tests="true" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

cd "${PROJECT_ROOT}"

info "检查项目与 Python 环境"
[[ -f requirements.txt && -f mcp_pipe.py && -f music_mcp_server.py ]] || fail "请在完整的 xiaozhi-music-mcp 仓库中运行本向导。"
command -v python3 >/dev/null 2>&1 || fail "未找到 python3，请先安装 Python 3.10 或更高版本。"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "Python 版本过低，需要 Python 3.10 或更高版本。"
printf 'Python：%s\n' "$(python3 --version 2>&1)"

info "创建虚拟环境并安装依赖"
if [[ ! -x "${VENV_PYTHON}" ]]; then
    python3 -m venv "${VENV_DIR}"
fi
"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -r requirements.txt

info "配置小智 MCP 接入点"
if [[ ! -f "${ENV_FILE}" ]]; then
    cp .env.example "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    printf '已从 .env.example 创建 .env。\n'
else
    chmod 600 "${ENV_FILE}"
    printf '检测到现有 .env，将保留已有配置。\n'
fi

endpoint="$(env_value MCP_ENDPOINT || true)"
if [[ -z "${endpoint}" || "${endpoint}" == *REPLACE_WITH_A_NEW_TOKEN* ]]; then
    read -r -s -p "粘贴小智控制台的 MCP 接入点（输入不会回显）：" endpoint
    printf '\n'
    [[ -n "${endpoint}" ]] || fail "MCP_ENDPOINT 不能为空。"
    "${VENV_PYTHON}" - "${endpoint}" <<'PY'
from urllib.parse import urlsplit
import sys

parts = urlsplit(sys.argv[1])
if parts.scheme not in {"ws", "wss"} or not parts.netloc:
    raise SystemExit("MCP 接入点必须是有效的 ws:// 或 wss:// 地址")
PY
    set_env_value MCP_ENDPOINT "${endpoint}"
else
    printf '检测到已配置的 MCP_ENDPOINT（已隐藏）。\n'
fi

set_env_value MUSIC_PROXY_PORT "8765"
printf '音频代理端口已固定为 8765。\n'

info "配置可选的飞书使用行为统计"
analytics_enabled="$(env_value FEISHU_ANALYTICS_ENABLED || true)"
if [[ "${analytics_enabled}" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss])$ ]] || \
    ask_yes_no "是否启用飞书多维表格行为统计？" "no"; then
    set_env_value FEISHU_ANALYTICS_ENABLED "true"
    set_env_value ANALYTICS_ENABLED "true"
    set_env_value FEISHU_AUTH_REQUIRED_ON_START "false"

    command -v lark-cli >/dev/null 2>&1 || \
        fail "启用飞书统计需要先安装 lark-cli；安装完成后重新运行部署向导。"
    set_env_value LARK_CLI_BIN "$(command -v lark-cli)"

    feishu_base_token="$(env_value FEISHU_BASE_TOKEN || true)"
    if [[ -z "${feishu_base_token}" ]]; then
        read -r -p "飞书多维表格 Base Token：" feishu_base_token
        [[ -n "${feishu_base_token}" ]] || fail "启用飞书统计时 FEISHU_BASE_TOKEN 不能为空。"
        set_env_value FEISHU_BASE_TOKEN "${feishu_base_token}"
    fi

    printf '即将启动飞书 CLI Device Flow，不需要配置 OAuth 回调地址。\n'
    "${VENV_PYTHON}" scripts/analytics_manager.py auth login
    "${VENV_PYTHON}" scripts/analytics_manager.py analytics init
    "${VENV_PYTHON}" scripts/analytics_manager.py analytics test
else
    set_env_value FEISHU_ANALYTICS_ENABLED "false"
    printf '已跳过飞书统计配置；以后可按 README 手动启用。\n'
fi

if [[ "${skip_tests}" == "false" ]]; then
    info "运行本地测试"
    "${VENV_PYTHON}" -m unittest -v \
        test_music_providers.py test_audio_proxy.py test_music_mcp_server.py \
        test_provider_manager.py test_netease_account.py test_music_search.py test_usage_analytics.py \
        test_lark_cli.py test_feishu_sync.py
    "${VENV_PYTHON}" test_mcp.py
    "${VENV_PYTHON}" test_mcp_pipe.py
else
    printf '已按参数跳过本地测试。\n'
fi

info "选择运行方式"
if [[ "$(uname -s)" == "Darwin" ]]; then
    if ask_yes_no "是否立即安装并启动 macOS 后台服务？" "yes"; then
        if ask_yes_no "是否启用登录自动启动？" "no"; then
            bash scripts/music_service.sh enable-autostart
        else
            bash scripts/music_service.sh disable-autostart
            bash scripts/music_service.sh start
        fi
        bash scripts/music_service.sh status
    else
        printf '稍后可运行：bash scripts/music_service.sh install\n'
    fi
else
    printf '当前后台服务脚本仅支持 macOS。前台启动命令：\n'
    printf '  source .venv/bin/activate && python mcp_pipe.py\n'
fi

cat <<'EOF'

部署向导执行完成。

验证顺序：
  1. 检查日志中出现“小智 MCP 接入点连接成功”。
  2. 在同一局域网检查新电脑 TCP 8765 可访问。
  3. 对设备说“播放乐鑫官方测试音频”。
  4. 再点播音乐源中的真实歌曲。

macOS 管理命令：
  bash scripts/music_service.sh status
  bash scripts/music_service.sh logs
EOF
