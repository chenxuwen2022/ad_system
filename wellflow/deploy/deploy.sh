#!/usr/bin/env bash
# ==============================================================================
# WellFlow 商拍系统 · 独立部署脚本
# 用法（代码已拉好之后）：
#   ./deploy.sh              # 完整部署：.env 校验 → build → alembic → up
#   ./deploy.sh --skip-db    # 跳过 alembic，只 build + 重启（表没变过时用）
#   ./deploy.sh --restart    # 只重启 app（最快，什么都不重新构建）
#
# 前置：
#   - 你已经 git pull 过了
#   - .env 在项目根目录 wellflow-saas-backend/.env（唯一一份）
#   - docker compose 在 PATH 里
# ==============================================================================
set -euo pipefail

MODE="full"
for arg in "$@"; do
    case "$arg" in
        --skip-db)  MODE="skip-db" ;;
        --restart)  MODE="restart" ;;
        --help|-h)
            echo "用法：./deploy.sh [--skip-db] [--restart]"
            echo "  （不带参数） 完整部署：build → alembic → up"
            echo "  --skip-db   跳过 alembic（只 build + up）"
            echo "  --restart   只重启 app 容器（最快）"
            exit 0
            ;;
    esac
done

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"   # wellflow/deploy
REPO_ROOT="$(cd "${DEPLOY_DIR}/../.." && pwd)"  # 项目根目录

echo "=============================================="
echo "  🚀  WellFlow 商拍部署脚本"
echo "  📁  deploy dir : ${DEPLOY_DIR}"
echo "  📁  repo root  : ${REPO_ROOT}"
echo "  🔧  mode       : ${MODE}"
echo "=============================================="

cd "$DEPLOY_DIR"

# docker compose 自身解析 yml 里的 ${VAR} 时不读 env_file，
# 必须显式 --env-file 指向根目录 .env，否则会用空值代替
COMPOSE="docker compose --env-file ${REPO_ROOT}/.env"

# ---------- [restart 模式] 最快：直接重启 ----------
if [ "$MODE" = "restart" ]; then
    echo ""
    echo "==> 只重启 app 容器…"
    $COMPOSE restart app
    echo "  ✅ 完成"
    exit 0
fi

# ---------- 1. .env 校验 ----------
echo ""
echo "==> 1/4 校验 .env…"
if [ ! -f "${REPO_ROOT}/.env" ]; then
    # 兜底：旧位置 wellflow/.env 还在就自动挪过来
    if [ -f "${REPO_ROOT}/wellflow/.env" ]; then
        mv "${REPO_ROOT}/wellflow/.env" "${REPO_ROOT}/.env"
        echo "  ↳ 从 wellflow/.env 迁到根目录"
    else
        echo "❌ 根目录 .env 不存在，也没找到 wellflow/.env"
        exit 1
    fi
fi
grep -q "POSTGRES_PASSWORD" "${REPO_ROOT}/.env" || { echo "❌ .env 缺少 POSTGRES_PASSWORD"; exit 1; }
echo "  ✅ .env 就绪（${REPO_ROOT}/.env）"

# ---------- 2. build ----------
echo ""
echo "==> 2/4 构建 app 镜像…"
$COMPOSE build app

# ---------- 3. alembic ----------
if [ "$MODE" != "skip-db" ]; then
    echo ""
    echo "==> 3/4 数据库迁移…"
    $COMPOSE up -d db
    echo "  等待 db 健康…"
    for _ in $(seq 1 20); do
        $COMPOSE exec -T db pg_isready -U postgres -d wellflow >/dev/null 2>&1 && break
        sleep 2
    done
    $COMPOSE exec -T app alembic upgrade head
    echo "  ✅ alembic upgrade head"
else
    echo ""
    echo "==> 3/4 跳过 alembic（--skip-db）"
fi

# ---------- 4. up + 健康检查 ----------
echo ""
echo "==> 4/4 启动服务…"
$COMPOSE up -d

echo ""
echo "==> 等待健康检查…"
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
        echo ""
        echo "=============================================="
        echo "  ✅ WellFlow 部署完成"
        curl -s "http://127.0.0.1:8000/health"
        echo ""
        echo "  📖 Swagger :  http://127.0.0.1:8000/docs"
        echo "  📖 ReDoc   :  http://127.0.0.1:8000/redoc"
        echo "=============================================="
        exit 0
    fi
    sleep 2
done

echo "❌ 健康检查超时，最近日志："
$COMPOSE logs --tail=50 app
exit 1
