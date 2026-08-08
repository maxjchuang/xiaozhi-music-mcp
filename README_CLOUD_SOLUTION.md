# ⚠️ 旧版云端方案

> 原方案的入站 WebSocket 方向不适用于小智 MCP 接入点。当前 Docker、Railway 和 Render 入口已经改为运行 `mcp_pipe.py`，部署时必须安全设置 `MCP_ENDPOINT`。具体步骤以 [README.md](README.md) 为准。

# ☁️ 小智AI免费音乐服务器 - 云端解决方案

## 🎯 解决方案概述

由于您的电脑无法24小时运行，我们为您准备了完整的云端部署方案，让音乐服务器可以24小时在线运行，完全免费！

## 📦 方案包含内容

### 🔧 核心文件
- `music_mcp_websocket_server.py` - 支持云端部署的WebSocket服务器
- `requirements.txt` - Python依赖包列表
- `Dockerfile` - Docker容器化配置
- `railway.json` - Railway.app部署配置
- `render.yaml` - Render.com部署配置

### 📚 部署指南
- `CLOUD_DEPLOYMENT_GUIDE.md` - 详细的云端部署指南
- `deploy_to_railway.md` - Railway.app快速部署指南
- `setup_github.sh` - GitHub仓库初始化脚本

### ⚙️ 配置文件
- `xiaozhi_cloud_config_template.json` - 云端版小智AI配置模板
- `xiaozhi_endpoint_config.json` - 本地版配置（备用）

## 🚀 快速开始（3步部署）

### 第1步：准备GitHub仓库
```bash
# 运行自动化脚本
./setup_github.sh

# 在GitHub创建仓库后推送代码
git remote add origin https://github.com/你的用户名/xiaozhi-music-mcp.git
git push -u origin main
```

### 第2步：部署到Railway（推荐）
1. 访问 [Railway.app](https://railway.app)
2. 使用GitHub登录
3. 点击 "New Project" → "Deploy from GitHub repo"
4. 选择您的音乐服务器仓库
5. 等待自动部署完成

### 第3步：配置小智AI
1. 复制Railway提供的域名（如：`your-app.up.railway.app`）
2. 在小智AI控制台添加WebSocket接入点：
   ```
   wss://your-app.up.railway.app
   ```
3. 使用 `xiaozhi_cloud_config_template.json` 中的配置

## 🌟 支持的云平台

| 平台 | 免费额度 | 部署难度 | 推荐指数 |
|------|----------|----------|----------|
| **Railway.app** | 500小时/月 | ⭐ 极简 | ⭐⭐⭐⭐⭐ |
| **Render.com** | 750小时/月 | ⭐⭐ 简单 | ⭐⭐⭐⭐ |
| **Fly.io** | 充足额度 | ⭐⭐⭐ 中等 | ⭐⭐⭐ |

## 🎵 功能特性

### 音乐控制
- 🔍 **智能搜索**：支持歌曲名、歌手、专辑搜索
- ▶️ **播放控制**：播放、暂停、停止、上一首、下一首
- 🔊 **音量调节**：0-100%精确音量控制

### 播放列表
- ➕ **添加歌曲**：将喜欢的歌曲加入播放列表
- 📋 **查看列表**：实时查看当前播放列表
- 🗑️ **清空列表**：一键清空播放列表

### 语音交互示例
- "小智，搜索周杰伦的歌"
- "播放青花瓷"
- "音量调到80%"
- "下一首歌"
- "暂停音乐"
- "添加这首歌到播放列表"

## 💰 费用说明

### 完全免费 🎉
- ✅ **服务器代码**：开源免费
- ✅ **云端部署**：使用免费tier
- ✅ **音乐功能**：无任何费用
- ✅ **24小时运行**：免费额度充足

### 免费额度对比
- **Railway.app**：500小时/月（约20天）
- **Render.com**：750小时/月（约31天）
- **Fly.io**：充足的免费额度

## 🔧 技术架构

```
小智AI音响 ←→ WebSocket(wss://) ←→ 云端MCP服务器
                                        ↓
                                   音乐搜索引擎
                                        ↓
                                   播放状态管理
```

## 📱 部署后的使用流程

1. **语音指令** → 小智AI音响接收
2. **MCP调用** → 通过WebSocket发送到云端服务器
3. **音乐处理** → 云端服务器处理音乐请求
4. **结果返回** → 通过WebSocket返回给小智AI
5. **语音反馈** → 小智AI音响播放结果

## 🆘 常见问题

### Q: 部署失败怎么办？
A: 检查 `requirements.txt` 和代码是否正确推送到GitHub

### Q: 无法连接WebSocket？
A: 确保使用 `wss://`（HTTPS）而不是 `ws://`（HTTP）

### Q: 免费额度用完了？
A: 可以切换到其他免费平台，或等待下月额度重置

### Q: 如何查看服务器状态？
A: 在云平台控制台查看日志和运行状态

## 🎉 完成后的效果

部署成功后，您将拥有：
- ✅ 24小时在线的音乐服务器
- ✅ 完整的音乐控制功能
- ✅ 无需本地电脑运行
- ✅ 完全免费的解决方案
- ✅ 稳定的云端服务

## 📞 技术支持

如果在部署过程中遇到问题：
1. 查看对应平台的官方文档
2. 检查服务器日志
3. 确认配置文件格式正确
4. 验证GitHub仓库代码完整性

---

🌟 **现在就开始部署，让您的小智AI音响拥有24小时在线的免费音乐功能！**
