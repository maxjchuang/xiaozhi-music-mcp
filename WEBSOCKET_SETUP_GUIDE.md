# ⚠️ 旧版入站 WebSocket 指南

> 小智 MCP 接入点需要本项目主动连接，不能把 `ws://localhost:8765` 配给小智云端。请以 [README.md](README.md) 为准。

# 免费音乐MCP WebSocket服务器 - 小智AI接入指南

## 🎵 项目概述

这是一个专为小智AI音响设计的免费音乐MCP WebSocket服务器，提供完整的音乐控制功能，包括搜索、播放、暂停、音量控制、播放列表管理等。

## ✅ 安装状态

- ✅ WebSocket MCP服务器已创建
- ✅ 依赖包已安装 (websockets, httpx, pydantic, aiofiles)
- ✅ 服务器功能测试通过
- ✅ 小智AI配置文件已生成

## 🚀 快速启动

### 1. 启动WebSocket服务器

```bash
cd /Users/a1234/Documents/music
python3 music_mcp_websocket_server.py
```

服务器将在 `ws://localhost:8765` 启动

### 2. 验证服务器运行

在新终端中运行测试：

```bash
python3 test_websocket_mcp.py
```

## 🔧 小智AI配置

### 方法1: 直接复制配置

将以下JSON配置直接复制到小智AI控制台的MCP接入点中：

```json
{
  "name": "免费音乐MCP服务器",
  "description": "为小智AI音响提供免费音乐搜索、播放控制、音量调节、播放列表管理等功能",
  "version": "1.0.0",
  "endpoint_url": "ws://localhost:8765",
  "transport": "websocket",
  "tools": [
    {
      "name": "search_music",
      "description": "搜索音乐，支持按歌曲名、歌手名、专辑名搜索",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "搜索关键词"},
          "limit": {"type": "integer", "description": "返回结果数量", "default": 10, "minimum": 1, "maximum": 50}
        },
        "required": ["query"]
      }
    },
    {
      "name": "play_music",
      "description": "播放指定的音乐",
      "inputSchema": {
        "type": "object",
        "properties": {
          "song_id": {"type": "string", "description": "歌曲ID"},
          "song_name": {"type": "string", "description": "歌曲名称"},
          "artist": {"type": "string", "description": "歌手名称"}
        },
        "required": ["song_id"]
      }
    },
    {
      "name": "pause_music",
      "description": "暂停当前播放的音乐",
      "inputSchema": {"type": "object", "properties": {}}
    },
    {
      "name": "resume_music",
      "description": "继续播放已暂停的音乐",
      "inputSchema": {"type": "object", "properties": {}}
    },
    {
      "name": "stop_music",
      "description": "停止播放音乐",
      "inputSchema": {"type": "object", "properties": {}}
    },
    {
      "name": "set_volume",
      "description": "设置音乐播放音量",
      "inputSchema": {
        "type": "object",
        "properties": {
          "volume": {"type": "integer", "description": "音量百分比，范围0-100", "minimum": 0, "maximum": 100}
        },
        "required": ["volume"]
      }
    },
    {
      "name": "add_to_playlist",
      "description": "将歌曲添加到播放列表",
      "inputSchema": {
        "type": "object",
        "properties": {
          "song_id": {"type": "string", "description": "歌曲ID"},
          "song_name": {"type": "string", "description": "歌曲名称"},
          "artist": {"type": "string", "description": "歌手名称"}
        },
        "required": ["song_id"]
      }
    },
    {
      "name": "get_playlist",
      "description": "获取当前播放列表",
      "inputSchema": {"type": "object", "properties": {}}
    },
    {
      "name": "clear_playlist",
      "description": "清空播放列表",
      "inputSchema": {"type": "object", "properties": {}}
    },
    {
      "name": "next_song",
      "description": "播放下一首歌曲",
      "inputSchema": {"type": "object", "properties": {}}
    },
    {
      "name": "previous_song",
      "description": "播放上一首歌曲",
      "inputSchema": {"type": "object", "properties": {}}
    }
  ]
}
```

### 方法2: 使用配置文件

也可以使用以下配置文件：
- `xiaozhi_endpoint_config.json` - 简化版配置
- `xiaozhi_websocket_mcp_config.json` - 完整版配置

### 配置步骤

1. 登录小智AI控制台
2. 进入MCP接入点管理
3. 点击"添加接入点"
4. 选择"WebSocket"传输协议
5. 粘贴上述JSON配置
6. 保存配置

## 🎤 语音交互示例

配置完成后，您可以通过小智AI音响使用以下语音指令：

### 音乐搜索
- "搜索周杰伦的歌曲"
- "找一些流行音乐"
- "搜索青花瓷"

### 播放控制
- "播放青花瓷"
- "暂停音乐"
- "继续播放"
- "停止播放"
- "下一首"
- "上一首"

### 音量控制
- "音量调到80"
- "声音小一点"
- "音量设置为50%"

### 播放列表管理
- "添加这首歌到播放列表"
- "显示播放列表"
- "清空播放列表"

## 🛠️ 技术架构

- **协议**: WebSocket (ws://localhost:8765)
- **传输格式**: JSON-RPC 2.0
- **支持功能**: 11个音乐控制工具 + 2个资源
- **音乐数据**: 模拟数据库（包含周杰伦、薛之谦等热门歌曲）

## 📁 项目文件

```
/Users/a1234/Documents/music/
├── music_mcp_websocket_server.py     # WebSocket MCP服务器
├── test_websocket_mcp.py             # 功能测试脚本
├── xiaozhi_endpoint_config.json      # 简化版配置文件
├── xiaozhi_websocket_mcp_config.json # 完整版配置文件
├── requirements.txt                   # 依赖包列表
├── README.md                         # 项目说明
├── USAGE_GUIDE.md                    # 使用指南
└── WEBSOCKET_SETUP_GUIDE.md          # 本文档
```

## 🔍 故障排除

### 服务器无法启动
```bash
# 检查端口是否被占用
lsof -i :8765

# 安装缺失依赖
pip3 install "websockets>=11.0.0"
```

### 连接失败
1. 确保WebSocket服务器正在运行
2. 检查防火墙设置
3. 验证端口8765是否可访问

### 小智AI无法识别
1. 检查JSON配置格式是否正确
2. 确认endpoint_url指向正确的服务器地址
3. 验证工具定义是否符合MCP规范

## 🚀 扩展开发

### 接入真实音乐API

可以将模拟数据库替换为真实的音乐API：

```python
# 示例：接入网易云音乐API
async def search_music_api(query: str, limit: int = 10):
    # 调用真实音乐API
    pass
```

### 添加新功能

```python
# 添加新的音乐控制工具
server.add_tool("shuffle_playlist", "随机播放", {
    "type": "object",
    "properties": {}
}, shuffle_playlist_handler)
```

## 📞 技术支持

如有问题，请检查：
1. WebSocket服务器日志
2. 小智AI控制台错误信息
3. 网络连接状态

## 📄 许可证

MIT License - 免费使用和修改

---

🎉 **恭喜！您的免费音乐MCP WebSocket服务器已准备就绪！**

现在您可以：
1. 保持WebSocket服务器运行
2. 在小智AI控制台添加上述配置
3. 开始享受语音音乐控制体验！
