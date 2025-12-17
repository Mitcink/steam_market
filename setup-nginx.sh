#!/bin/bash
# Steam Buff爬虫Nginx配置脚本

CONFIG_NAME="steam-buff"
CONFIG_FILE="/etc/nginx/sites-available/$CONFIG_NAME"
ENABLED_LINK="/etc/nginx/sites-enabled/$CONFIG_NAME"

echo "正在设置Steam Buff爬虫的Nginx配置..."

# 备份原有配置（如果存在）
if [ -f "$CONFIG_FILE" ]; then
    BACKUP_FILE="$CONFIG_FILE.backup.$(date +%Y%m%d_%H%M%S)"
    sudo cp "$CONFIG_FILE" "$BACKUP_FILE"
    echo "原有配置已备份到: $BACKUP_FILE"
fi

echo "请选择配置方式:"
echo "1. 使用域名 (如: crawler.your-domain.com)"
echo "2. 使用IP地址"
read -p "请输入选项 [1/2]: " choice

case $choice in
    1)
        read -p "请输入域名: " domain
        SERVER_NAME="$domain"
        ;;
    2)
        read -p "请输入IP地址: " ip
        SERVER_NAME="$ip"
        ;;
    *)
        echo "无效选项，默认使用IP配置"
        SERVER_NAME="_"
        ;;
esac

# 创建Nginx配置文件
sudo tee "$CONFIG_FILE" > /dev/null << EOF
# Steam Buff爬虫服务配置
server {
    listen 80;
    server_name $SERVER_NAME;

    # 日志文件
    access_log /var/log/nginx/steam-buff-access.log;
    error_log /var/log/nginx/steam-buff-error.log;

    # 安全设置
    client_max_body_size 10M;

    # API接口代理
    location /api/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # 超时设置（爬虫可能耗时较长）
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    # 主页面代理
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # 禁止访问隐藏文件和敏感文件
    location ~ /\. {
        deny all;
        access_log off;
        log_not_found off;
    }
}
EOF

echo "配置文件已创建: $CONFIG_FILE"

# 创建软链接到sites-enabled目录
if [ ! -L "$ENABLED_LINK" ]; then
    sudo ln -s "$CONFIG_FILE" "$ENABLED_LINK"
    echo "已创建软链接: $ENABLED_LINK"
else
    echo "软链接已存在，跳过创建"
fi

# 测试Nginx配置
echo "测试Nginx配置..."
if sudo nginx -t; then
    echo "Nginx配置测试成功!"

    echo "请选择操作:"
    echo "1. 重新加载Nginx（不中断服务）"
    echo "2. 重启Nginx"
    echo "3. 仅测试，不做任何操作"
    read -p "请输入选项 [1/2/3]: " action

    case $action in
        1)
            sudo systemctl reload nginx
            echo "Nginx配置已重新加载"
            ;;
        2)
            sudo systemctl restart nginx
            echo "Nginx服务已重启"
            ;;
        3)
            echo "仅测试完成，未修改运行中的Nginx配置"
            ;;
        *)
            echo "无效选项，请手动执行: sudo systemctl reload nginx"
            ;;
    esac

    echo ""
    echo "✅ 配置完成!"
    echo "访问地址: http://$SERVER_NAME"
    echo "Docker容器将运行在: http://localhost:5000"
else
    echo "❌ Nginx配置测试失败，请检查配置文件"
    exit 1
fi