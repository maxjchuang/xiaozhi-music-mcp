# 小智音乐 MCP 服务

这是一个运行在个人电脑或云主机上的小智外部 MCP 服务。程序通过小智控制台提供的 WebSocket 接入点主动连接小智云端，为 EchoEar（喵伴）的设备端在线音乐工具解析音频直链。

> 当前版本用于打通首条真实播放链路：外部 MCP 只返回乐鑫官方测试音频 URL，真正的播放由 EchoEar 固件内置的 `self.online_music.play_music` 执行。

## 工作方式

```text
music_mcp_server.py（解析 URL）
        ↕ stdio
    mcp_pipe.py ↔ 小智云端 ↔ EchoEar 的 self.online_music.play_music（播放）
```

- `mcp_pipe.py` 主动连接 `MCP_ENDPOINT`，因此本地运行时不需要公网 IP 或端口映射。
- `music_mcp_server.py` 是标准 FastMCP stdio 服务。
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
```

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
```

然后回到小智控制台刷新 MCP 接入点，应能看到在线状态和 1 个工具：`resolve_music_url`。

角色人物介绍应加入：

```text
收到音乐相关需求时，禁止使用 search_music、官方 play_music 和 self.music.play_song。
先调用外部 MCP 工具 resolve_music_url 获得音频 URL。
解析成功后，必须立即调用设备端 MCP 工具 self.online_music.play_music，
并原样使用 resolve_music_url 返回的 device_arguments。
```

必要时重启小智设备，再尝试：

- “播放乐鑫官方测试音频”

停止服务请按 `Ctrl+C`。

## 本地测试

不连接小智也可以验证标准 MCP 握手和工具调用：

```bash
source .venv/bin/activate
python test_mcp.py
```

直接运行 `python music_mcp_server.py` 时程序会等待 stdio MCP 请求，这属于正常现象；日常接入小智应运行 `mcp_pipe.py`。

## 可用工具

| 工具 | 功能 |
|---|---|
| `resolve_music_url` | 将白名单测试音频解析为直链，并返回 EchoEar 设备工具所需参数 |

## 当前限制

- 仅开放乐鑫官方 MP3 测试音频，不提供商业歌曲目录。
- EchoEar 的 URL 播放仍可能经过 Nologo 在线音乐后台，并受设备端 `config_music_player_enabled`、账号或名额限制。
- 后续接入真实音乐服务时，只扩展解析器即可；设备播放工具保持不变。

## 安全说明

- `MCP_ENDPOINT` 中的 Token 相当于凭据，不要上传、截图或写进日志。
- 桥接程序输出地址时会隐藏查询参数中的 Token。
- 如果 Token 曾提交到公开仓库，仅删除当前文件不够；还应撤销 Token，并按需要清理 Git 历史。
