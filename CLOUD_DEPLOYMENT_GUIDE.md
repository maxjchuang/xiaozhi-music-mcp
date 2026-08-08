# ⚠️ 旧版云端部署指南

> 本文的公开入站 WebSocket 配置已经过时。当前部署进程应主动连接 `MCP_ENDPOINT`，请以 [README.md](README.md) 和最新部署配置为准。

# 🚀 免费音乐MCP服务器云端部署指南

由于您的电脑无法24小时运行，我们为您准备了多种免费云端部署方案，让音乐服务器可以24小时在线运行。

## 📋 部署前准备

1. **创建GitHub账号**（如果还没有）
2. **上传代码到GitHub**
3. **选择云平台进行部署**

## 🌟 推荐方案：Railway.app（最简单）

### 优势
- ✅ 完全免费（每月500小时免费额度）
- ✅ 支持WebSocket
- ✅ 自动从GitHub部署
- ✅ 提供HTTPS域名
- ✅ 零配置部署

### 部署步骤

1. **访问 [Railway.app](https://railway.app)**
2. **使用GitHub账号登录**
3. **点击 "New Project"**
4. **选择 "Deploy from GitHub repo"**
5. **选择您的音乐服务器仓库**
6. **Railway会自动检测Python项目并部署**
7. **部署完成后，点击项目获取公网域名**

### 获取WebSocket地址
部署成功后，您会得到类似这样的地址：
```
wss://your-app-name.up.railway.app
```

## 🔧 方案二：Render.com

### 优势
- ✅ 免费tier可用
- ✅ 支持WebSocket
- ✅ 自动SSL证书

### 部署步骤

1. **访问 [Render.com](https://render.com)**
2. **使用GitHub账号注册/登录**
3. **点击 "New +" → "Web Service"**
4. **连接GitHub仓库**
5. **配置如下：**
   - Name: `xiaozhi-music-mcp`
   - Environment: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python3 music_mcp_websocket_server.py`
   - Plan: `Free`

## 🐳 方案三：Fly.io

### 优势
- ✅ 免费额度充足
- ✅ 全球CDN
- ✅ 支持Docker

### 部署步骤

1. **安装Fly CLI**
   ```bash
   curl -L https://fly.io/install.sh | sh
   ```

2. **登录Fly.io**
   ```bash
   flyctl auth login
   ```

3. **在项目目录运行**
   ```bash
   flyctl launch
   ```

4. **按提示完成配置**

## 📱 更新小智AI配置

部署成功后，您需要更新小智AI的MCP配置：

### 原配置（本地）
```json
{
  "endpoint": "ws://localhost:8765"
}
```

### 新配置（云端）
```json
{
  "endpoint": "wss://your-app-name.up.railway.app"
}
```

**注意：** 
- 本地使用 `ws://`（HTTP）
- 云端使用 `wss://`（HTTPS）

## 🔍 验证部署

部署完成后，您可以通过以下方式验证：

1. **访问云端地址**，应该看到WebSocket连接
2. **在小智AI中测试音乐功能**
3. **检查云平台的日志**

## 💡 使用建议

1. **Railway.app** - 最推荐，部署最简单
2. **Render.com** - 备选方案，稳定性好
3. **Fly.io** - 高级用户选择，功能最强大

## 🆘 常见问题

### Q: 部署后无法连接？
A: 检查防火墙设置，确保使用 `wss://` 而不是 `ws://`

### Q: 免费额度用完了怎么办？
A: 可以切换到其他免费平台，或者升级到付费计划

### Q: 如何查看服务器日志？
A: 在各云平台的控制台都有日志查看功能

## 🎉 完成！

部署成功后，您的音乐MCP服务器将24小时在线运行，无需本地电脑持续开机！

---

**需要帮助？** 请查看各平台的官方文档或联系技术支持。
