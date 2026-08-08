# 音乐源配置

服务端严格按照 `MUSIC_PROVIDER_ORDER` 顺序搜索，默认顺序为：

```text
Navidrome → 网易云（账号授权）→ Jamendo → 非官方适配器
```

某个音乐源返回至少一首歌曲后立即停止搜索。只有无结果、超时或请求失败时才会继续下一个来源。

单个 Provider 的搜索与播放地址解析总超时默认为 20 秒，可按网络情况调整：

```dotenv
MUSIC_PROVIDER_TIMEOUT_SECONDS=20
```

## Navidrome

Navidrome 管理用户自己拥有的音乐文件，并提供兼容 OpenSubsonic 的 API。

```dotenv
NAVIDROME_URL=http://127.0.0.1:4533
NAVIDROME_USERNAME=xiaozhi
NAVIDROME_PASSWORD=请替换
```

推荐为小智创建单独的只读用户。服务使用 Subsonic Token Authentication，密码不会发送到设备，也不会出现在播放 URL 和日志里。输出统一请求 MP3 192 kbps，Navidrome 会在需要时转码。

如果 Navidrome 运行在其他电脑或 NAS 上，`NAVIDROME_URL` 必须是运行本服务的电脑可以访问的地址。

## Jamendo

注册 Jamendo 开发者账号并创建应用，获得 Client ID：

```dotenv
JAMENDO_CLIENT_ID=请替换
```

服务使用官方 `GET /v3.0/tracks` 搜索接口和返回的 `audio` 流媒体地址，音频格式请求为 MP3 VBR。Jamendo 曲库主要是独立音乐，不能替代主流中文商业曲库。

## 网易云（受限模式）

通过本机运行的 NeteaseCloudMusicApiEnhanced 接入：

```dotenv
NETEASE_PROVIDER_ENABLED=true
NETEASE_API_URL=http://127.0.0.1:3000
NETEASE_API_TIMEOUT_SECONDS=12
```

API 服务必须仅监听 `127.0.0.1`，并固定配置：

```dotenv
HOST=127.0.0.1
ENABLE_GENERAL_UNBLOCK=false
```

Provider 使用 `/cloudsearch` 搜索，随后按搜索顺序调用 `/song/url/v1` 请求 `standard` 音质，找到第一条可播放结果后立即返回。它不会发送 `unblock=true`，也不会调用 `/song/url/match`。网易云返回的原生完整歌曲和官方试听 URL 都可播放，试听结果会标记 `is_preview=true`；无 URL 的歌曲会继续检查下一条结果。

如需使用自己账号已获授权的内容，可把 `MUSIC_U=...` 形式的 Cookie 只保存在网易云 API 服务的本地环境文件中。不要把 Cookie 写入本项目、日志或设备 URL。

当前 macOS 本机安装使用以下路径：

```text
服务目录：~/.local/share/xiaozhi/netease-api-enhanced
服务配置：~/.local/share/xiaozhi/netease-api-enhanced/.env
开机自启：~/Library/LaunchAgents/com.xiaozhi.netease-api.plist
运行日志：~/.local/state/xiaozhi/logs/netease-api.log
```

可用下面的命令查看状态：

```bash
launchctl print gui/$(id -u)/com.xiaozhi.netease-api
curl 'http://127.0.0.1:3000/cloudsearch?keywords=海阔天空&type=1&limit=1'
```

## 非官方适配器

主服务不直接绑定某个抓取项目，而是通过一个隔离的 HTTP JSON 适配器接入，默认关闭：

```dotenv
UNOFFICIAL_PROVIDER_ENABLED=true
UNOFFICIAL_PROVIDER_URL=http://127.0.0.1:9000/search
UNOFFICIAL_PROVIDER_TOKEN=可选Bearer令牌
```

主服务会发送：

```http
GET /search?q=歌曲名&limit=5
Authorization: Bearer <token>
```

适配器返回 JSON：

```json
{
  "tracks": [
    {
      "id": "source-track-id",
      "title": "歌曲名",
      "artist": "歌手",
      "album": "专辑",
      "duration": 240,
      "audio_url": "https://example.test/song.mp3",
      "content_type": "audio/mpeg",
      "artwork_url": "https://example.test/cover.jpg"
    }
  ]
}
```

只有 `id`、`title`、`artist`、`audio_url` 是核心字段。`audio_url` 必须使用 HTTP 或 HTTPS。

非官方音乐平台接口可能失效，并可能受账号、会员、地区、版权及平台服务条款约束。适配器应只访问用户有权播放的内容，并独立管理 Cookie；不要把 Cookie 或账号密码返回给本服务。

## 动态音频代理

MCP 工具解析出歌曲后，会通过仅允许本机访问、带随机密钥的注册接口登记真实音频地址。EchoEar 收到的是类似下面的短期地址：

```text
http://192.168.x.x:8765/stream/<随机令牌>
```

令牌默认 30 分钟失效，可通过 `MUSIC_PROXY_STREAM_TTL` 调整。代理支持 HTTP Range，并且不会把 Navidrome 鉴权参数或上游地址发给设备。
