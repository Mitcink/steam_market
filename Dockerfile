# 使用Python 3.9-slim
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py

# 安装系统依赖 (curl用于调试, tzdata用于时区)
RUN apt-get update && apt-get install -y \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 设置时区为上海
ENV TZ=Asia/Shanghai

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 关键：先创建data目录并设置权限
# 这样即使挂载卷，基础权限也是正确的（视宿主机情况而定）
RUN mkdir -p /app/data && \
    chown -R 1000:1000 /app

# 创建并切换到非root用户
RUN useradd -m -u 1000 appuser
USER appuser

# 暴露端口
EXPOSE 5000

# 启动命令优化：
# 1. 使用 --workers 1：保证只有一个进程，这样 app.py 中的全局变量 (crawler_thread) 才能生效。
# 2. 使用 --threads 4：处理并发请求，避免爬虫阻塞网页加载。
# 3. --timeout 120：爬虫请求可能会稍微卡顿主线程，增加超时时间。
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "120", "app:app"]