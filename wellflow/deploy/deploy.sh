#!/usr/bin/env bash
# ==============================================================================
# WellFlow 商拍系统 · 独立部署脚本
# 用法：
#   ./deploy.sh                # 完整部署：.env 校验 → [智能 build] → alembic → up
#   ./deploy.sh --skip-build   # 跳过镜像构建（已构建过、只跑迁移时用）
#   ./deploy.sh --force-build  # 强制重建镜像（清缓存，Dockerfile/依赖大变时用）
#   ./deploy.sh --skip-db      # 跳过 alembic（表结构没变、只重启时用）
#   ./deploy.sh --restart      # 只重启容器（最快，什么都不 build 也不跑迁移）
#
# 前置：
#   - 你已经 git pull 过了
#   - .env 在项目根目录 wellflow-saas-backend/.env（唯一一份）
#   - docker compose 在 PATH 里
# ==============================================================================
set -euo pipefail

SKIP_BUILD=false
FORCE_BUILD=false
SKIP_DB=false
MODE="full"

for arg in "$@"; do
    case "$arg" in
        --skip-build)  SKIP_BUILD=true ;;
        --force-build) FORCE_BUILD=true ;;
        --skip-db)     SKIP_DB=true ;;
        --restart)     MODE="restart" ;;
        --help|-h)
            echo "用法：./deploy.sh [选项...]"
            echo ""
            echo "  （不带参数）完整部署：智能 build → alembic → up"
            echo "  --skip-build   跳过镜像构建（已构建过时用）"
            echo "  --force-build  强制重建镜像（清缓存）"
            echo "  --skip-db      跳过 alembic（表结构没变）"
            echo "  --restart      只重启容器（最快）"
            echo ""
            echo "  可组合：--skip-build --skip-db = 只 up 不 build 不迁移"
            exit 0
            ;;
    esac
done

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"   # wellflow/deploy
REPO_ROOT="$(cd "${DEPLOY_DIR}/../.." && pwd)"  # 项目根目录
WELLFLOW_DIR="${REPO_ROOT}/wellflow"

echo "=============================================="
echo "  🚀  WellFlow 商拍部署脚本"
echo "  📁  repo root  : ${REPO_ROOT}"
echo "  �  选项       : build=$([ $SKIP_BUILD = true ] && echo "skip" || echo "run") $( [ $FORCE_BUILD = true ] && echo "(force)" || true )  alembic=$([ $SKIP_DB = true ] && echo "skip" || echo "run")  restart=$([ $MODE = "restart" ] && echo "yes" || echo "no")"
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
echo "==> 1/3 校验 .env…"
if [ ! -f "${REPO_ROOT}/.env" ]; then
    if [ -f "${REPO_ROOT}/wellflow/.env" ]; then
        mv "${REPO_ROOT}/wellflow/.env" "${REPO_ROOT}/.env"
        echo "  ↳ 从 wellflow/.env 迁到根目录"
    else
        echo "❌ 根目录 .env 不存在"
        exit 1
    fi
fi
grep -q "POSTGRES_PASSWORD" "${REPO_ROOT}/.env" || { echo "❌ .env 缺少 POSTGRES_PASSWORD"; exit 1; }
echo "  ✅ .env 就绪"

# ---------- 2. build ----------
if [ "$SKIP_BUILD" = false ]; then
    echo ""
    echo "==> 2/3 构建 app 镜像…"

    # 智能检测：镜像已存在且源码/依赖没改过 → 跳过
    IMAGE_EXISTS=$($COMPOSE images -q app 2>/dev/null || true)
    if [ "$FORCE_BUILD" = false ] && [ -n "$IMAGE_EXISTS" ]; then
        # 跨平台：Linux 用 stat -c %Y，macOS 用 stat -f %m
        stat_mtime() {
            stat -c "%Y" "$1" 2>/dev/null || stat -f "%m" "$1" 2>/dev/null || echo 0
        }
        # 取镜像构建时间（unix epoch）
        IMAGE_CREATED=$(docker inspect -f '{{.Created}}' "$IMAGE_EXISTS" 2>/dev/null || echo "")
        if [ -n "$IMAGE_CREATED" ]; then
            # 把 RFC3339 时间转 unix timestamp（Linux: date -d, macOS: date -j -f）
            IMAGE_EPOCH=$(date -d "${IMAGE_CREATED%.*}Z" +%s 2>/dev/null \
                       || date -j -f "%Y-%m-%dT%H:%M:%SZ" "${IMAGE_CREATED%.*}Z" +%s 2>/dev/null \
                       || echo 0)
            # 取源码里最新 mtime（Dockerfile / requirements.txt / wellflow/app / wellflow/migration）
            NEWEST_SRC=0
            while IFS= read -r f; do
                [ -z "$f" ] && continue
                t=$(stat_mtime "$f")
                [ "$t" -gt "$NEWEST_SRC" ] && NEWEST_SRC=$t
            done < <(find "${WELLFLOW_DIR}/deploy/Dockerfile" \
                           "${WELLFLOW_DIR}/requirements.txt" \
                           "${WELLFLOW_DIR}/app" \
                           "${WELLFLOW_DIR}/migration" \
                           -type f 2>/dev/null)

            if [ "$NEWEST_SRC" -lt "$IMAGE_EPOCH" ] 2>/dev/null; then
                echo "  ↳ 源码未变，复用已有镜像（加 --force-build 可强制重建）"
                SKIP_BUILD=true
            fi
        fi
    fi

    if [ "$SKIP_BUILD" = false ]; then
        if [ "$FORCE_BUILD" = true ]; then
            $COMPOSE build --no-cache app
        else
            $COMPOSE build app
        fi
        echo "  ✅ 镜像构建完成"
    fi
else
    echo ""
    echo "==> 2/3 跳过构建（--skip-build）"
fi

# ---------- 3. alembic ----------
if [ "$SKIP_DB" = false ]; then
    echo ""
    echo "==> 3/3 数据库迁移…"
    $COMPOSE up -d db
    echo "  等待 db 健康…"
    for _ in $(seq 1 20); do
        $COMPOSE exec -T db pg_isready -U postgres -d wellflow >/dev/null 2>&1 && break
        sleep 2
    done
    $COMPOSE run --rm app alembic upgrade head
    echo "  ✅ alembic upgrade head"
else
    echo ""
    echo "==> 3/3 跳过 alembic（--skip-db）"
fi

# ---------- up + 健康检查 ----------
echo ""
echo "==> 启动服务…"
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
