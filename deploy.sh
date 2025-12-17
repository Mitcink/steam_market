#!/bin/bash
# Steam Buff爬虫部署脚本

echo "=== Steam Buff爬虫部署脚本 ==="
echo "当前目录: $(pwd)"

# 检查Docker是否安装
if ! command -v docker &> /dev/null; then
    echo "❌ Docker未安装，请先安装Docker"
    exit 1
fi

# 检查Docker Compose是否安装
if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose未安装，请先安装Docker Compose"
    exit 1
fi

# 创建数据目录
if [ ! -d "data" ]; then
    echo "创建数据目录..."
    mkdir -p data
    echo "数据目录已创建: $(pwd)/data"
fi

# 检查必要的文件
if [ ! -f "requirements.txt" ]; then
    echo "❌ requirements.txt 文件不存在"
    exit 1
fi

if [ ! -f "Dockerfile" ]; then
    echo "❌ Dockerfile 文件不存在"
    exit 1
fi

if [ ! -f "docker-compose.yml" ]; then
    echo "❌ docker-compose.yml 文件不存在"
    exit 1
fi

echo "开始部署..."

# 停止并删除现有容器
echo "1. 停止并清理现有容器..."
docker-compose down 2>/dev/null

# 构建Docker镜像
echo "2. 构建Docker镜像..."
docker-compose build

# 启动服务
echo "3. 启动Docker容器..."
docker-compose up -d

# 检查容器状态
echo "4. 检查容器状态..."
sleep 3

if docker-compose ps | grep -q "Up"; then
    echo "✅ 容器启动成功!"

    # 显示容器信息
    echo ""
    echo "=== 容器信息 ==="
    docker-compose ps

    # 显示日志
    echo ""
    echo "=== 日志（最后20行）==="
    docker-compose logs --tail=20

    # 显示访问信息
    echo ""
    echo "=== 访问信息 ==="
    echo "本地访问: http://localhost:5000"
    echo "API状态: http://localhost:5000/api/status"
    echo "数据目录: $(pwd)/data"
    echo ""
    echo "✅ 部署完成!"

    # 检查服务是否响应
    echo "测试服务响应..."
    sleep 2
    if curl -s http://localhost:5000/api/status > /dev/null; then
        echo "✅ 服务响应正常"
    else
        echo "⚠️ 服务启动中，可能需要更多时间..."
    fi
else
    echo "❌ 容器启动失败，请查看日志:"
    docker-compose logs
    exit 1
fi