FROM python:3.9-slim

WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 程序主动连接小智 MCP_ENDPOINT，不需要暴露入站端口
CMD ["python3", "mcp_pipe.py", "music_mcp_server.py"]
