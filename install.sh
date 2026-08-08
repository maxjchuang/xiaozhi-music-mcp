#!/bin/bash

# 免费音乐MCP服务器安装脚本
# 适用于小智AI音响

echo "🎵 免费音乐MCP服务器安装程序"
echo "================================"

# 检查Python版本
echo "📋 检查Python环境..."
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    echo "✅ Python版本: $PYTHON_VERSION"
else
    echo "❌ 错误: 未找到Python3，请先安装Python 3.8+"
    exit 1
fi

# 检查pip
if command -v pip3 &> /dev/null; then
    echo "✅ pip3已安装"
else
    echo "❌ 错误: 未找到pip3，请先安装pip"
    exit 1
fi

# 安装依赖包
echo "📦 安装Python依赖包..."
if pip3 install -r requirements.txt; then
    echo "✅ 依赖包安装成功"
else
    echo "❌ 依赖包安装失败，请检查网络连接"
    exit 1
fi

# 设置执行权限
echo "🔧 设置文件权限..."
chmod +x music_mcp_server.py
chmod +x mcp_pipe.py

# 测试服务器
echo "🧪 测试MCP服务器..."
echo "正在启动测试（5秒后自动停止）..."
# 在macOS上使用gtimeout或者简单的python测试
if command -v gtimeout >/dev/null 2>&1; then
    gtimeout 5s python3 music_mcp_server.py &
    TEST_PID=$!
    sleep 2
    if ps -p $TEST_PID > /dev/null; then
        echo "✅ MCP服务器测试成功"
        kill $TEST_PID 2>/dev/null
    else
        echo "❌ MCP服务器测试失败，请检查配置"
        exit 1
    fi
else
    # 简单的语法检查
    python3 -m py_compile music_mcp_server.py && echo "✅ MCP服务器语法检查通过" || echo "❌ MCP服务器语法检查失败"
fi

# 显示配置信息
echo ""
echo "🎉 安装完成！"
echo "================================"
echo "📍 服务器路径: $(pwd)/music_mcp_server.py"
echo "🔌 桥接程序: $(pwd)/mcp_pipe.py"
echo ""
echo "📖 下一步操作:"
echo "1. 登录 xiaozhi.me 控制台"
echo "2. 在智能体配置页面复制新的 MCP 接入点"
echo "3. 设置环境变量: export MCP_ENDPOINT='wss://...新Token'"
echo "4. 启动桥接: python3 mcp_pipe.py"
echo ""
echo "🎵 现在您可以对小智AI说："
echo "   - '搜索周杰伦的歌曲'"
echo "   - '播放青花瓷'"
echo "   - '暂停音乐'"
echo "   - '音量调到80'"
echo ""
echo "📚 详细使用说明请查看 README.md"
echo "================================"
