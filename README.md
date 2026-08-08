# 小智音乐 MCP 服务

这是一个运行在个人电脑或云主机上的小智外部 MCP 服务。程序通过小智控制台提供的 WebSocket 接入点主动连接小智云端，为 EchoEar（喵伴）的设备端在线音乐工具搜索歌曲并生成局域网播放地址。

音乐源严格按 `Navidrome → 网易云（账号授权）→ Jamendo → 可选非官方适配器` 的顺序降级。真正的播放仍由 EchoEar 固件内置的 `self.online_music.play_music` 执行。

## 工作方式

```text
music_mcp_server.py（按优先级搜索歌曲）
        ↕ stdio
    mcp_pipe.py ↔ 小智云端 ↔ EchoEar 的 self.online_music.play_music（播放）
        ↳ :8765/stream/<临时令牌>（动态音频代理）
```

- `mcp_pipe.py` 主动连接 `MCP_ENDPOINT`，因此本地运行时不需要公网 IP 或端口映射。
- `music_mcp_server.py` 是标准 FastMCP stdio 服务。
- `mcp_pipe.py` 会在局域网启动动态音频代理，隐藏上游鉴权信息并解决部分 ESP32 无法直连 HTTPS/CDN 的问题；默认端口为 `8765`。
- Provider 配置和非官方适配器协议见 [PROVIDERS.md](PROVIDERS.md)。
- 电脑必须保持开机、联网，桥接程序必须持续运行。

## 1. 获取新的 MCP 接入点

1. 登录 [xiaozhi.me](https://xiaozhi.me)。
2. 进入对应设备或智能体的“配置角色”页面。
3. 点击“MCP 接入点”，复制 `wss://api.xiaozhi.me/mcp/?token=...` 地址。
4. 如果曾经使用过本仓库旧配置中的 Token，请在控制台撤销它并生成新 Token。

不要把真实接入点提交到 Git。

## 2. 安装

要求 Python 3.10 或更高版本。

```bash
cd /Users/bytedance/Projects/github/xiaozhi-music-mcp
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
MUSIC_PROVIDER_ORDER=navidrome,jamendo,unofficial

NAVIDROME_URL=http://127.0.0.1:4533
NAVIDROME_USERNAME=你的用户名
NAVIDROME_PASSWORD=你的密码

JAMENDO_CLIENT_ID=你的ClientID
```

当前 EchoEar 测试固件固定允许端口 `8765`，请勿修改该值。

至少配置 Navidrome、网易云或 Jamendo 中的一个。网易云和非官方适配器默认关闭；详细配置见 [PROVIDERS.md](PROVIDERS.md)。乐鑫官方测试音频作为诊断入口始终保留，不依赖音乐源配置。

`.env` 已加入 `.gitignore`。

也可以只在当前终端设置：

```bash
export MCP_ENDPOINT='wss://api.xiaozhi.me/mcp/?token=你的新Token'
```

## 4. 启动

```bash
source .venv/bin/activate
python mcp_pipe.py
```

成功时会看到：

```text
连接小智 MCP 接入点：wss://api.xiaozhi.me/mcp/?token=***
小智 MCP 接入点连接成功
已启动本地 MCP 服务：.../music_mcp_server.py
动态音乐局域网代理已启动：http://局域网IP:8765/stream/<临时令牌>
```

然后回到小智控制台刷新 MCP 接入点，应能看到在线状态和 1 个工具：`resolve_music_url`。小智不需要了解各个 Provider，来源选择由服务端完成。

角色人物介绍应加入：

```text
收到音乐相关需求时，禁止使用 search_music、官方 play_music 和 self.music.play_song。
先调用外部 MCP 工具 resolve_music_url 搜索歌曲并获得音频 URL。
解析成功后，必须立即调用设备端 MCP 工具 self.online_music.play_music，
并原样使用 resolve_music_url 返回的 device_arguments。
```

必要时重启小智设备，再尝试：

- “播放乐鑫官方测试音频”
- “播放海阔天空 Beyond”（需要相应音乐源中存在该歌曲）

停止服务请按 `Ctrl+C`。

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

- Navidrome 只管理用户自己的音乐文件；网易云 Provider 接受平台原生完整歌曲或官方试听 URL；Jamendo 以独立音乐为主。
- 非官方适配器默认关闭，稳定性、账号权限和内容合规性由适配器使用者负责。
- EchoEar 与运行 MCP 的电脑必须在同一局域网，且本机防火墙需允许 Python 接收 TCP 8765 端口的局域网连接。
- EchoEar 的 URL 播放仍可能经过 Nologo 在线音乐后台，并受设备端 `config_music_player_enabled`、账号或名额限制。
- 当前自动选择每个 Provider 返回的第一条结果；重名歌曲建议在语音请求中同时说明歌手。

## 安全说明

- `MCP_ENDPOINT` 中的 Token 相当于凭据，不要上传、截图或写进日志。
- Navidrome 密码、网易云 Cookie、Jamendo Client ID 和非官方适配器令牌只放在本地环境文件，不要提交到 Git。
- 桥接程序输出地址时会隐藏查询参数中的 Token。
- 如果 Token 曾提交到公开仓库，仅删除当前文件不够；还应撤销 Token，并按需要清理 Git 历史。
