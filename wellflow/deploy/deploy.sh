#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# WellFlow 后端一键部署脚本（在服务器上执行）
# 用法：./deploy.sh
# 说明：不包含 git 提交/拉取，假设代码已同步到当前目录
# ============================================================

# 服务器对外 IP（或域名），可运行时覆盖：SERVER_IP=1.2.3.4 ./deploy.sh
SERVER_IP="${SERVER_IP:-192.168.110.254}"

cd "$(dirname "$0")"

echo "==> 1/4 校验 .env"
if [ ! -f ../.env ]; then
  echo "❌ 缺少 ../.env（wellflow/ 根目录下），请先创建（至少需要 POSTGRES_PASSWORD / LLM_OFOX_API_KEY / LAOZHANG_API_KEY）"
  exit 1
fi
grep -q "POSTGRES_PASSWORD" ../.env || { echo "❌ ../.env 缺少 POSTGRES_PASSWORD"; exit 1; }

echo "==> 2/4 构建镜像"
docker compose build --no-cache

echo "==> 3/4 更新并启动容器（Alembic 迁移在容器启动时自动执行）"
docker compose pull db 2>/dev/null || true
docker compose up -d --remove-orphans

echo "==> 4/4 等待服务就绪"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8000/health" > /dev/null 2>&1; then
    echo "✅ 部署完成"
    curl -s "http://127.0.0.1:8000/health"
    echo
    echo "对外地址：http://${SERVER_IP}:8000"
    exit 0
  fi
  sleep 2
done

echo "❌ 健康检查超时，最近日志如下："
docker compose logs --tail=50 app
exit 1