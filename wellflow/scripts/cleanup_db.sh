#!/usr/bin/env bash
# ==============================================================================
# 线上数据库清理 —— 执行入口
# 依赖：docker compose 已在服务器上装好，db service 正常运行
# ==============================================================================
# 用法（任选其一，都能跑）：
#   cd ~/wellflow-saas-backend/wellflow && bash scripts/cleanup_db.sh
#   cd ~/wellflow-saas-backend/wellflow/scripts && bash cleanup_db.sh
#   绝对路径直接跑
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SQL_FILE="${SCRIPT_DIR}/cleanup_db.sql"

# 关键：cd 到 deploy 目录再执行 docker compose
# 这样 docker-compose.yml 里 env_file: ../.env 的相对路径才能正确解析
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/../deploy" && pwd)"
cd "${DEPLOY_DIR}"

echo "📦 docker compose 目录: ${DEPLOY_DIR}"

# 1. 预览当前所有表
echo ""
echo "===== 当前数据库中的表 ====="
docker compose exec -T db \
    psql -U postgres -d wellflow -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"

# 2. 确认
echo ""
read -rp "确认执行清理？(输入 YES 继续，其他退出): " confirm
if [[ "${confirm}" != "YES" ]]; then
    echo "已取消"
    exit 0
fi

# 3. 执行清理 SQL
echo ""
echo "===== 执行清理 SQL ====="
docker compose exec -T db \
    psql -U postgres -d wellflow -v ON_ERROR_STOP=1 < "${SQL_FILE}"

echo ""
echo "✅ 清理完成"
