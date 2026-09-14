#!/usr/bin/env bash
# =============================================================
# 数语深流 · 千川素材投放台 —— Linux 一键部署脚本
# 适用：Ubuntu 20.04+ / Debian 11+ / CentOS 7+（CentOS 需自行替换包管理命令）
# 用法：
#   1) 把项目代码放到服务器（scp/rsync/git clone 均可），进入项目目录
#   2) sudo bash deploy.sh
# 可配置环境变量（按需前置）：
#   APP_USER     运行用户           默认 ad_system
#   APP_DIR      项目目录           默认 /opt/ad_system
#   MATERIAL_DIR 素材目录           默认 /data/test素材
#   PORT         应用端口           默认 8000
#   WITH_NGINX   是否装 nginx 反代   默认 1（1=装 0=不装，无域名时建议 0）
# =============================================================
set -e

APP_USER="${APP_USER:-ad_system}"
APP_DIR="${APP_DIR:-/opt/ad_system}"
MATERIAL_DIR="${MATERIAL_DIR:-/data/test素材}"
PORT="${PORT:-8000}"
WITH_NGINX="${WITH_NGINX:-1}"

echo "======================================================"
echo " 部署参数：APP_DIR=$APP_DIR  MATERIAL_DIR=$MATERIAL_DIR  PORT=$PORT"
echo "======================================================"

# ---------- 1. 系统依赖 ----------
echo "[1/8] 安装系统依赖…"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip curl
    [ "$WITH_NGINX" = "1" ] && apt-get install -y nginx
elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip curl
    [ "$WITH_NGINX" = "1" ] && yum install -y nginx
else
    echo "无法识别包管理器，请手动安装 Python3.10+"; exit 1
fi

# ---------- 2. 创建运行用户与目录 ----------
echo "[2/8] 创建运行用户 $APP_USER 与目录…"
id -u "$APP_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$APP_USER"
mkdir -p "$APP_DIR" "$MATERIAL_DIR" "$APP_DIR/media_storage"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR" "$MATERIAL_DIR"

# ---------- 3. 定位项目代码 ----------
echo "[3/8] 检查项目代码…"
# 若 APP_DIR 下还没有代码（脚本与代码同目录时自动复制过去）
if [ ! -f "$APP_DIR/main.py" ]; then
    SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$SRC_DIR/main.py" ]; then
        echo "  -> 从 $SRC_DIR 同步代码到 $APP_DIR"
        rsync -a --exclude venv --exclude __pycache__ --exclude .git "$SRC_DIR"/ "$APP_DIR"/
    else
        echo "!! 未找到 main.py。请先把项目代码放到 $APP_DIR 后重新运行。"; exit 1
    fi
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# ---------- 4. Python 虚拟环境与依赖 ----------
echo "[4/8] 创建虚拟环境并安装依赖…"
cd "$APP_DIR"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# ---------- 5. 素材目录配置 ----------
echo "[5/8] 配置素材目录（$MATERIAL_DIR）…"
# 通过环境变量注入，代码无需改动；systemd 服务文件里已带该变量

# ---------- 6. systemd 服务 ----------
echo "[6/8] 注册 systemd 服务…"
cat > /etc/systemd/system/ad_system.service <<EOF
[Unit]
Description=Ad System (FastAPI + Qianchuan)
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=LOCAL_MATERIAL_DIR=$MATERIAL_DIR
ExecStart=$APP_DIR/venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3
# SQLite 单进程写入：禁止多 worker，保证数据一致
# 如未来换 PostgreSQL 可调整此处

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ad_system
systemctl restart ad_system
sleep 3
systemctl --no-pager status ad_system | head -n 8 || true

# ---------- 7. Nginx 反向代理（可选） ----------
if [ "$WITH_NGINX" = "1" ]; then
    echo "[7/8] 配置 Nginx 反向代理…"
    SERVER_NAME="${SERVER_NAME:-_}"
    cat > /etc/nginx/conf.d/ad_system.conf <<EOF
server {
    listen 80;
    server_name $SERVER_NAME;

    client_max_body_size 200m;   # 素材上传大小上限

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;   # 千川拉数据较慢，放宽超时
        proxy_send_timeout 300s;
    }
}
EOF
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl enable nginx && systemctl restart nginx
    echo "  -> 已启用 Nginx（80 端口），有域名后配置 HTTPS 即可"
else
    echo "[7/8] 跳过 Nginx（WITH_NGINX=0）"
fi

# ---------- 8. 防火墙 ----------
echo "[8/8] 防火墙放行…"
if command -v ufw >/dev/null 2>&1; then
    ufw allow 80/tcp >/dev/null 2>&1 || true
    ufw allow 443/tcp >/dev/null 2>&1 || true
    [ "$WITH_NGINX" = "1" ] || ufw allow "$PORT"/tcp >/dev/null 2>&1 || true
    echo "  -> ufw 已放行 80/443（未开 8000 对外）"
fi

echo "======================================================"
echo " 部署完成！"
echo "  - 应用： http://服务器IP/  （Nginx） 或 http://服务器IP:$PORT/ （直连）"
echo "  - 日志： journalctl -u ad_system -f"
echo "  - 素材目录：$MATERIAL_DIR（把素材文件放进去即可）"
echo "  - 注意：首次使用需在页面「设置」里确认账户；token 过期需重新授权"
echo "======================================================"
