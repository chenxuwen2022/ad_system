#!/usr/bin/env bash
# ==============================================================================
# 线上数据库清理 —— 执行入口
# 依赖：docker compose 已在服务器上装好，且 db 容器名为 `db`
# ==============================================================================
# 用法：
#   cd /path/to/wellflow-saas-backend/wellflow/deploy
#   bash ../../scripts/cleanup_db.sh
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SQL_FILE="${SCRIPT_DIR}/cleanup_db.sql"

# 找到 docker-compose.yml 所在目录
DEPLOY_DIR="$(find "$SCRIPT_DIR/.." -name docker-compose.yml -type f | head -1 | xargs dirname)"
if [[ -z "$DEPLOY_DIR" ]]; then
    echo "❌ 找不到 docker-compose.yml"
    exit 1
fi

echo "📦 docker compose 目录: ${DEPLOY_DIR}"

# 1. 预览当前所有表
echo ""
echo "===== 当前数据库中的表 ====="
docker compose -f "${DEPLOY_DIR}/docker-compose.yml" exec -T db \
    psql -U postgres -d wellflow -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"

# 2. 备份（跳过可选：确认无 PG 密码环境变量的话提示手动备份）
if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
    echo ""
    echo "⚠️  未检测到 POSTGRES_PASSWORD 环境变量。如果还没备份，建议先手动执行："
    echo "    pg_dump -U postgres -d wellflow -h 127.0.0.1 -p 15432 > backup.sql"
    read -rp "按 Enter 继续执行清理，或 Ctrl+C 退出: "
fi

# 3. 执行清理 SQL
echo ""
echo "===== 执行清理 SQL ====="
docker compose -f "${DEPLOY_DIR}/docker-compose.yml" exec -T db \
    psql -U postgres -d wellflow -v ON_ERROR_STOP=1 < "${SQL_FILE}"

echo ""
echo "✅ 清理完成"
