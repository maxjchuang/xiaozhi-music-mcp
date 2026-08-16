# 小智音乐 MCP 服务

这是一个运行在个人电脑或云主机上的小智外部 MCP 服务。程序通过小智控制台提供的 WebSocket 接入点主动连接小智云端，为 EchoEar（喵伴）的设备端在线音乐工具搜索歌曲并生成局域网播放地址。

音乐源默认按 `Navidrome → 网易云完整歌曲 → Fangpi → Jamendo → 可选非官方适配器` 的顺序降级。真正的播放仍由 EchoEar 固件内置的 `self.online_music.play_music` 执行。

搜索默认启用 MCP 侧智能候选排序：标准化 ASR 文本，根据标题、拼音、歌手和版本要求评分，再只解析高分候选的播放权限。30 秒试听会保留并标记，高相关试听优先于无关完整歌曲。可在 [`config/music_query_aliases.json`](config/music_query_aliases.json) 中维护高频 ASR 纠错，详细设计见 [`docs/SMART_MUSIC_SEARCH_DESIGN.md`](docs/SMART_MUSIC_SEARCH_DESIGN.md)。

## 工作方式

```text
music_mcp_server.py（按优先级搜索歌曲）
        ↕ stdio
    mcp_pipe.py ↔ 小智云端 ↔ EchoEar 的 self.online_music.play_music（播放）
        ↳ :8765/media/<临时令牌>/audio（动态音频代理）
        ↳ :8765/media/<临时令牌>/manifest.json（歌曲信息、封面与歌词）
```

- `mcp_pipe.py` 主动连接 `MCP_ENDPOINT`，因此本地运行时不需要公网 IP 或端口映射。
- `music_mcp_server.py` 是标准 FastMCP stdio 服务。
- `mcp_pipe.py` 会在局域网启动动态音频代理，隐藏上游鉴权信息并解决部分 ESP32 无法直连 HTTPS/CDN 的问题；默认端口为 `8765`。
- 新版代理会为每首歌生成短期媒体清单。封面按需裁剪为 360 × 360 暗化背景和 192 × 192 唱片；可用歌词以 LRC 转发。旧 `/stream/<令牌>` 地址仍兼容。
- Provider 配置和非官方适配器协议见 [PROVIDERS.md](PROVIDERS.md)。
- 使用行为记录和飞书仪表盘的架构见 [飞书行为分析设计](docs/FEISHU_ANALYTICS_DESIGN.md)，当前实施进度见 [代码实施计划](docs/FEISHU_ANALYTICS_IMPLEMENTATION_PLAN.md)。
- 电脑必须保持开机、联网，桥接程序必须持续运行。

## 1. 获取新的 MCP 接入点

1. 登录 [xiaozhi.me](https://xiaozhi.me)。
2. 进入对应设备或智能体的“配置角色”页面。
3. 点击“MCP 接入点”，复制 `wss://api.xiaozhi.me/mcp/?token=...` 地址。
4. 如果曾经使用过本仓库旧配置中的 Token，请在控制台撤销它并生成新 Token。

不要把真实接入点提交到 Git。

## 2. 安装

要求 Python 3.10 或更高版本。

### 部署向导（推荐）

在仓库目录运行一个命令：

```bash
bash scripts/deploy_wizard.sh
```

向导会依次完成：

- 检查 Python 版本并创建 `.venv`；
- 安装依赖；
- 保留已有 `.env` / `.env.local`，缺少时创建配置并安全读取 `MCP_ENDPOINT`；
- 固定 EchoEar 所需的音频代理端口 `8765`；
- 运行 Provider、音频代理、标准 MCP 和 WebSocket 桥接测试；
- 在 macOS 上可选择立即安装后台服务以及是否登录自启动。

如已单独完成测试，可使用：

```bash
bash scripts/deploy_wizard.sh --skip-tests
```

向导不会安装或迁移 Navidrome、网易云 API 等独立服务，它们仍需按 [PROVIDERS.md](PROVIDERS.md) 配置。

### 手动安装

```bash
cd xiaozhi-music-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. 配置

推荐使用 `.env`：

```bash
cp .env.example .env
```

编辑 `.env`，把占位地址换成刚生成的接入点：

```dotenv
MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=你的新Token
LOG_LEVEL=INFO
MUSIC_PROXY_PORT=8765
MUSIC_PROVIDER_ORDER=navidrome,netease,fangpi,jamendo,unofficial

NAVIDROME_URL=http://127.0.0.1:4533
NAVIDROME_USERNAME=你的用户名
NAVIDROME_PASSWORD=你的密码

JAMENDO_CLIENT_ID=你的ClientID

FANGPI_PROVIDER_ENABLED=true
FANGPI_API_TIMEOUT_SECONDS=10
```

当前 EchoEar 测试固件固定允许端口 `8765`，请勿修改该值。

Fangpi 默认启用，因此未配置其他音乐源时仍会尝试搜索；如果 Cloudflare 拒绝独立客户端，可按 [PROVIDERS.md](PROVIDERS.md) 手动配置浏览器 Cookie。网易云和通用非官方适配器默认关闭。乐鑫官方测试音频作为诊断入口始终保留，不依赖音乐源配置。

`.env` 已加入 `.gitignore`。

也可以只在当前终端设置：

```bash
export MCP_ENDPOINT='wss://api.xiaozhi.me/mcp/?token=你的新Token'
```

## 4. 启动

### 后台服务（推荐）

首次安装运行：

```bash
bash scripts/music_service.sh install
```

安装程序会询问：

```text
是否启用登录自动启动？[y/N]
```

默认选择 `N`：服务立即在 macOS LaunchAgent 中运行，关闭终端或退出 Codex 后仍会继续工作，但下次登录不会自动启动。选择 `Y` 则同时开启登录自启动。

启动命令会读取当前 Provider 配置，先自动拉起需要本地进程的托管 Provider，再启动 MCP。停止、重启、状态、自启动切换和日志查看也会统一管理这些进程。网易云本机端点默认托管；Navidrome 或通用适配器只有显式配置 `*_SERVICE_MANAGED=true` 时才由本项目拉起。Fangpi 与 Jamendo 是远程 HTTP 来源，不会创建本地进程。

Provider 采用故障隔离：单个托管 Provider 拉起失败只会输出警告，不会阻止 MCP 启动；搜索时也会自动继续尝试后续来源。启动命令只有在项目安装、环境文件或 Provider 配置语法本身无效时才会失败。

日常管理命令：

```bash
bash scripts/music_service.sh start
bash scripts/music_service.sh update
bash scripts/music_service.sh stop
bash scripts/music_service.sh restart
bash scripts/music_service.sh status
bash scripts/music_service.sh enable-autostart
bash scripts/music_service.sh disable-autostart
bash scripts/music_service.sh logs
```

`update` 会在干净的 `main` 分支上快进到 GitHub `origin/main`，自动创建或复用 `.venv`、升级安装 `requirements.txt` 中的依赖，并运行完整回归测试。服务原本在运行时会在全部验证成功后自动重启；服务原本已停止时保持停止，随后可直接运行 `start`。为保护本地改动，工作区不干净、当前不在 `main` 或无法快进时会停止更新。

`disable-autostart` 不会中断正在运行的服务，只会阻止它在下次登录时自动启动。`stop` 不会改变自启动设置。

### 前台运行

```bash
source .venv/bin/activate
python mcp_pipe.py
```

成功时会看到：

```text
连接小智 MCP 接入点：wss://api.xiaozhi.me/mcp/?token=***
小智 MCP 接入点连接成功
已启动本地 MCP 服务：.../music_mcp_server.py
动态音乐局域网代理已启动：http://局域网IP:8765/media/<临时令牌>/audio
```

然后回到小智控制台刷新 MCP 接入点，应能看到在线状态和 1 个工具：`resolve_music_url`。小智不需要了解各个 Provider，来源选择由服务端完成。

角色人物介绍应加入：

```text
收到音乐相关需求时，禁止使用 search_music、官方 play_music 和 self.music.play_song。
先调用外部 MCP 工具 resolve_music_url 搜索歌曲并获得音频 URL。
解析成功后，必须立即调用设备端 MCP 工具 self.online_music.play_music，
并原样使用 resolve_music_url 返回的 device_arguments。
```

新版 EchoEar 固件会同时接收可选的 `device_arguments.metadata_url`，用于显示歌名、歌手、暗化封面、旋转唱片和三行同步歌词。旧固件和旧 URL 播放流程保持兼容。

必要时重启小智设备，再尝试：

- “播放乐鑫官方测试音频”
- “播放海阔天空 Beyond”（需要相应音乐源中存在该歌曲）

前台运行时，停止服务请按 `Ctrl+C`。

## 飞书使用行为统计（可选）

启用后，搜索、Provider 结果和设备首次请求音频等事件会先写入本地 SQLite，再由后台 Worker 异步同步到飞书多维表格。飞书断网或登录失效不会影响音乐播放，授权恢复后会自动补传。

### 1. 安装并登录飞书 CLI

先安装官方 `lark-cli`，并确认命令可用：

```bash
lark-cli --version
```

项目使用 CLI 的 Device Flow 登录，不需要创建本地回调服务器，也不需要在项目中配置 App ID、App Secret、Access Token 或 Refresh Token。首次使用时，管理命令会在需要时执行 CLI 配置初始化，并申请 Base 业务域权限。登录用户仍需对目标多维表格具有可管理权限。

### 2. 配置并登录

在 `.env.local` 中配置：

```dotenv
ANALYTICS_ENABLED=true
ANALYTICS_TRANSCRIPT_MODE=masked

FEISHU_ANALYTICS_ENABLED=true
FEISHU_AUTH_REQUIRED_ON_START=false
LARK_CLI_BIN=
FEISHU_BASE_TOKEN=多维表格URL中base/后面的Token
```

完成首次登录和初始化：

```bash
bash scripts/music_service.sh auth login
bash scripts/music_service.sh analytics init
bash scripts/music_service.sh analytics test
```

`auth login` 会调用 `lark-cli auth login --domain base`，按 CLI 提示完成 Device Flow 授权。Token 的存储和刷新由 CLI 管理，项目不会读取 Token。`analytics init` 会创建或校验“原始事件”表和“小智使用分析”仪表盘，并将表 ID、仪表盘 ID 写入权限为 `0600` 的 `.env`。

### 3. 日常管理

```bash
bash scripts/music_service.sh auth status
bash scripts/music_service.sh analytics status
bash scripts/music_service.sh analytics sync
bash scripts/music_service.sh analytics retry
```

交互式执行 `start` 时，如果尚未登录，会自动进入登录流程。LaunchAgent 等后台启动不会等待浏览器；它会记录 `AUTH_REQUIRED`，保留本地事件并继续提供音乐服务。只有显式设置 `FEISHU_AUTH_REQUIRED_ON_START=true` 时，授权失败才会阻止启动。

当前版本能完整统计 MCP 可观察到的音乐搜索和开始播放行为。普通对话、唤醒、自然播放结束、换歌和音频欠载需要 EchoEar 固件增加遥测上报后才能准确记录。

## 迁移到另一台电脑

不需要修改 EchoEar 固件。新电脑通过 `MCP_ENDPOINT` 主动连接小智云端，但动态音频地址使用新电脑的局域网 IP，因此新电脑和 EchoEar 必须处于同一局域网。

### 1. 切换前检查

- 新电脑安装 Python 3.10 或更高版本，并保持开机、联网且不会自动睡眠；
- 防火墙允许 Python 接收局域网 TCP `8765`；
- 路由器没有开启客户端隔离；
- 如果启用了 VPN，确认日志显示的是 EchoEar 可以访问的局域网 IPv4 地址；
- 使用同一个 `MCP_ENDPOINT` 时，先停止旧电脑服务，避免两个桥接程序同时占用同一个接入点。

推荐在小智控制台生成新的 MCP 接入点 Token，切换成功后撤销旧 Token。不要通过 Git、聊天记录或公开网盘迁移 Token 和 Cookie。

### 2. 获取代码并迁移本地配置

```bash
git clone git@github.com:maxjchuang/xiaozhi-music-mcp.git
cd xiaozhi-music-mcp
```

通过加密传输、隔空投送或其他可信方式，把旧电脑项目目录中的 `.env` 和 `.env.local` 复制到新电脑相同位置，然后限制权限：

```bash
chmod 600 .env .env.local
bash scripts/deploy_wizard.sh
```

如果不迁移旧配置，部署向导会创建 `.env` 并要求输入新的 `MCP_ENDPOINT`，其他 Provider 按 `.env.example` 和 [PROVIDERS.md](PROVIDERS.md) 配置。

### 3. 迁移网易云音乐服务

启用网易云 Provider 时，新电脑还必须独立运行 `NeteaseCloudMusicApiEnhanced`：

```bash
mkdir -p ~/.local/share/xiaozhi
cd ~/.local/share/xiaozhi
git clone --filter=blob:none \
  https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced.git \
  netease-api-enhanced
cd netease-api-enhanced
npm install
```

其本地 `.env` 至少保持以下约束：

```dotenv
HOST=127.0.0.1
PORT=3000
ENABLE_GENERAL_UNBLOCK=false
```

网易云登录态保存在这个独立服务的 `.env` 中，而不是本仓库。可以安全迁移原来的 `NETEASE_COOKIE=MUSIC_U=...`，也可以在新电脑重新扫码登录。启动后先验证搜索接口：

已完成 Provider 配置后，可以通过统一服务命令管理网易云账号：

```bash
# 查看当前登录状态
bash scripts/music_service.sh netease status

# 扫码登录；成功后自动保存 Cookie 并重载 Provider
bash scripts/music_service.sh netease login

# 退出并清除本机 Cookie
bash scripts/music_service.sh netease logout

# 退出当前账号后立即扫码登录新账号
bash scripts/music_service.sh netease relogin
```

`login` 和 `relogin` 会自动打开二维码图片，且不会在终端或日志中输出完整 Cookie。

也可以手动启动 API 并验证搜索接口：

```bash
cd ~/.local/share/xiaozhi/netease-api-enhanced
npm start
```

保持该终端运行，并在另一个终端执行：

```bash
curl --fail --get \
  --data-urlencode 'keywords=海阔天空 Beyond' \
  --data 'type=1' \
  --data 'limit=1' \
  http://127.0.0.1:3000/cloudsearch
```

验证成功后，macOS 上可交给本项目的统一服务管理器持续运行；Linux 仍需使用 `systemd` 或其他进程管理器。

本仓库对应配置为：

```dotenv
NETEASE_PROVIDER_ENABLED=true
NETEASE_API_URL=http://127.0.0.1:3000
NETEASE_SERVICE_MANAGED=true
NETEASE_SERVICE_DIR=~/.local/share/xiaozhi/netease-api-enhanced
NETEASE_SERVICE_COMMAND=["npm","start"]
FANGPI_PROVIDER_ENABLED=false
```

Navidrome、Jamendo 和其他适配器只需迁移自己实际启用的配置。若 Navidrome 位于 NAS 或另一台电脑，`NAVIDROME_URL` 必须改成新电脑可以访问的地址，不能继续使用错误的 `127.0.0.1`。本机 Navidrome 如需统一托管，可按 [PROVIDERS.md](PROVIDERS.md) 配置其服务目录和启动命令。

### 4. 接管服务

先在旧电脑停止服务：

```bash
bash scripts/music_service.sh stop
```

然后在新电脑启动。macOS 推荐：

```bash
bash scripts/music_service.sh install
bash scripts/music_service.sh update
bash scripts/music_service.sh status
bash scripts/music_service.sh logs
```

Linux 可以先以前台方式验证：

```bash
source .venv/bin/activate
python mcp_pipe.py
```

Windows 目前不支持 Bash 部署向导和 `music_service.sh`，可以在 PowerShell 中使用 `.venv\Scripts\python.exe mcp_pipe.py` 前台验证，再配置任务计划。当前 `music_service.sh` 只支持 macOS；其他系统验证成功后需自行配置 `systemd` 或任务计划。Docker 部署还需要确保返回给 EchoEar 的不是容器内部 IP，因此普通端口映射不适合作为首次迁移验证方式。

### 5. 分层验证

先验证本地代码，不连接小智也能执行：

```bash
source .venv/bin/activate
python -m unittest -v test_music_providers.py test_audio_proxy.py
python test_mcp.py
python test_mcp_pipe.py
```

启动服务后，状态和日志应包含：

```text
运行状态：运行中
音频代理：正在监听 TCP 8765
小智 MCP 接入点连接成功
已启动本地 MCP 服务
动态音乐局域网代理已启动：http://新电脑局域网IP:8765/media/<临时令牌>/audio
```

可以从同一局域网的另一台电脑测试端口：

```bash
nc -vz 新电脑局域网IP 8765
```

最后进行两级设备验证：

1. 对 EchoEar 说“播放乐鑫官方测试音频”，验证小智 MCP、局域网代理和设备 URL 播放链路；
2. 再说“播放海阔天空 Beyond”，验证实际 Provider、账号权限、完整歌曲过滤和代理播放。

成功时服务日志会出现来自设备的请求，例如：

```text
[audio-proxy] "GET /stream/... HTTP/1.1" 200
```

支持 HTTP Range 的播放请求也可能返回 `206`。如果控制台显示 MCP 在线但设备不能播放，优先检查日志中的代理 IP、TCP `8765` 防火墙以及两台设备是否确实处于同一局域网。

## 本地测试

不连接小智也可以验证标准 MCP 握手和工具调用：

```bash
source .venv/bin/activate
python -m unittest -v test_music_providers.py test_audio_proxy.py
python test_mcp.py
python test_mcp_pipe.py
```

直接运行 `python music_mcp_server.py` 时程序会等待 stdio MCP 请求，这属于正常现象；日常接入小智应运行 `mcp_pipe.py`。

## 可用工具

| 工具 | 功能 |
|---|---|
| `resolve_music_url` | 按 Provider 优先级搜索歌曲，生成短期局域网地址并返回 EchoEar 设备工具所需参数 |

## 当前限制

- Navidrome 只管理用户自己的音乐文件；网易云 Provider 仅接受平台原生完整歌曲并过滤 30 秒试听；Fangpi 是默认启用但可能变化的非官方网页源；Jamendo 以独立音乐为主。
- 非官方适配器默认关闭，稳定性、账号权限和内容合规性由适配器使用者负责。
- EchoEar 与运行 MCP 的电脑必须在同一局域网，且本机防火墙需允许 Python 接收 TCP 8765 端口的局域网连接。
- EchoEar 的 URL 播放仍可能经过 Nologo 在线音乐后台，并受设备端 `config_music_player_enabled`、账号或名额限制。
- 当前自动选择每个 Provider 返回的第一条结果；重名歌曲建议在语音请求中同时说明歌手。

## 安全说明

- `MCP_ENDPOINT` 中的 Token 相当于凭据，不要上传、截图或写进日志。
- Navidrome 密码、网易云 Cookie、Jamendo Client ID 和非官方适配器令牌只放在本地环境文件，不要提交到 Git。
- 桥接程序输出地址时会隐藏查询参数中的 Token。
- 如果 Token 曾提交到公开仓库，仅删除当前文件不够；还应撤销 Token，并按需要清理 Git 历史。
