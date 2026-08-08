# ⚠️ 旧版使用指南

> 本文记录旧版入站 WebSocket/stdio 演示，不能直接用于小智云端接入。请以 [README.md](README.md) 的 `MCP_ENDPOINT + mcp_pipe.py` 主动连接方式为准。

# 🎵 免费音乐MCP服务器 - 使用指南

## 📋 项目概述

这是一个专为小智AI音响设计的免费音乐MCP（Model Context Protocol）服务器，提供完整的音乐控制功能，包括搜索、播放、暂停、音量控制和播放列表管理等。

## 🌟 部署方式选择

### 💻 本地部署（需要电脑24小时开机）
- 适合：有24小时运行电脑的用户
- 优势：完全本地控制，无网络延迟
- 缺点：需要电脑持续运行

### ☁️ 云端部署（推荐）
- 适合：电脑无法24小时运行的用户
- 优势：24小时在线，无需本地电脑运行
- 平台：Railway.app、Render.com、Fly.io
- 费用：完全免费

## ✅ 安装状态

- ✅ 服务器文件已创建
- ✅ 依赖包已安装
- ✅ 配置文件已生成
- ✅ 功能测试已通过

## 🔧 配置信息

### MCP服务器配置
- **服务器路径**: `/Users/a1234/Documents/music/music_mcp_server.py`
- **启动命令**: `python3 /Users/a1234/Documents/music/music_mcp_server.py`
- **传输协议**: `stdio`
- **服务器名称**: `music-mcp-server`
- **版本**: `1.0.0`

### 小智AI接入配置
```json
{
  "name": "免费音乐MCP服务器",
  "description": "为小智AI音响提供免费音乐控制服务",
  "version": "1.0.0",
  "command": "python3",
  "args": ["/Users/a1234/Documents/music/music_mcp_server.py"],
  "transport": "stdio"
}
```

## 🚀 配置步骤

### 📱 方案一：本地部署配置

#### 1. 登录小智AI控制台
- 访问: https://xiaozhi.me
- 登录您的账户

#### 2. 进入智能体配置
- 选择您的智能体
- 进入"MCP接入点"配置页面

#### 3. 添加MCP接入点
- **名称**: 免费音乐MCP服务器
- **命令**: `python3`
- **参数**: `/Users/a1234/Documents/music/music_mcp_server.py`
- **传输**: `stdio`

### ☁️ 方案二：云端部署配置（推荐）

#### 1. 部署到云端
请参考 [云端部署指南](CLOUD_DEPLOYMENT_GUIDE.md) 进行部署

#### 2. 配置小智AI
- **接入点类型**: WebSocket
- **URL**: `wss://your-app-name.up.railway.app`
- **配置文件**: 使用 `xiaozhi_cloud_config_template.json`

#### 3. 快速部署
```bash
# 运行GitHub设置脚本
./setup_github.sh

# 推送到GitHub后，访问Railway.app进行一键部署
```

### 4. 保存并测试
- 保存配置
- 重启智能体服务

## 🎵 支持的音乐功能

### 🔍 音乐搜索
- **功能**: 搜索音乐曲目
- **语音指令**: "搜索周杰伦的歌曲"
- **参数**: 搜索关键词、结果数量限制

### ▶️ 播放控制
- **播放音乐**: "播放青花瓷"
- **暂停音乐**: "暂停音乐"
- **继续播放**: "继续播放"
- **停止音乐**: "停止音乐"

### 🔊 音量控制
- **设置音量**: "音量调到80"
- **支持范围**: 0-100%

### 📝 播放列表管理
- **添加到播放列表**: "把这首歌加到播放列表"
- **查看播放列表**: "显示播放列表"
- **清空播放列表**: "清空播放列表"

### ⏭️ 切换歌曲
- **下一首**: "下一首歌"
- **上一首**: "上一首歌"

## 🎯 语音交互示例

```
用户: "小智，搜索周杰伦的歌曲"
小智: "搜索 '周杰伦' 的结果：
      1. 青花瓷 - 周杰伦
      2. 稻香 - 周杰伦
      ..."

用户: "播放青花瓷"
小智: "正在播放: 青花瓷 - 周杰伦"

用户: "音量调到70"
小智: "音量已设置为: 70%"

用户: "暂停音乐"
小智: "音乐已暂停"
```

## 🛠️ 技术架构

### 核心组件
- **MCP服务器**: 基于标准MCP协议
- **音乐搜索**: 模拟音乐数据库
- **播放状态管理**: 内存状态跟踪
- **工具接口**: 11个音乐控制工具

### 数据结构
```python
# 播放状态
playback_state = {
    "is_playing": False,
    "current_song": None,
    "volume": 50,
    "position": 0,
    "playlist": []
}
```

## 🔧 故障排除

### 常见问题

1. **服务器无法启动**
   - 检查Python环境: `python3 --version`
   - 检查依赖包: `pip3 list | grep -E "httpx|pydantic|aiofiles"`
   - 重新安装: `./install.sh`

2. **小智AI无法连接**
   - 确认路径正确: `/Users/a1234/Documents/music/music_mcp_server.py`
   - 检查文件权限: `ls -la music_mcp_server.py`
   - 测试服务器: `python3 test_mcp.py`

3. **音乐搜索无结果**
   - 当前使用模拟数据
   - 可扩展接入真实音乐API

### 日志查看
```bash
# 手动启动查看日志
python3 music_mcp_server.py

# 测试服务器功能
python3 test_mcp.py
```

## 🚀 扩展开发

### 接入真实音乐API
1. 替换 `search_music_api` 函数
2. 接入网易云音乐、QQ音乐等API
3. 添加音乐播放URL获取

### 添加新功能
1. 歌词显示
2. 音乐推荐
3. 收藏管理
4. 历史记录

## 📞 技术支持

- **项目路径**: `/Users/a1234/Documents/music/`
- **配置文件**: `xiaozhi_mcp_config.json`
- **测试工具**: `test_mcp.py`
- **安装脚本**: `install.sh`

## 📄 许可证

MIT License - 免费使用和修改

---

🎉 **恭喜！您的免费音乐MCP服务器已经准备就绪！**

现在您可以在小智AI控制台中配置MCP接入点，开始享受语音音乐控制的便利了！
