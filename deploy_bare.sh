#!/usr/bin/env bash
# ==============================================================================
# 裸机部署脚本 —— wellflow-saas-backend（广告投放 ad/ + 电商商拍 wellflow/）
#
# 文件位置：项目根目录 deploy_bare.sh（与 Docker 版 deploy.sh 并存，互不影响）
#
# 用法：
#   ./deploy_bare.sh                # 完整部署（依赖 -> 迁移 -> token_store.db 检查 -> systemd 启动）
#   ./deploy_bare.sh --skip-deps    # 跳过依赖安装（已装过时用）
#   ./deploy_bare.sh --skip-migrate # 跳过 wellflow 数据库迁移（表结构没变时用）
#   ./deploy_bare.sh --restart      # 只重启 systemd 服务
#
# 前置：
#   - Linux 服务器（CentOS/Rocky 用 yum/dnf，Debian/Ubuntu 用 apt）
#   - 已安装 python3.12 / python3-venv / git / PostgreSQL（本地 5432 或远程）
#   - 项目代码已放到服务器（git clone 或上传），脚本在项目根目录运行
#   - .env 已在项目根目录（可用 .env.example 复制后填写）
# ==============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="wellflow-saas"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
VENV_DIR="${REPO_ROOT}/.venv"
PORT="${PORT:-8000}"
SKIP_DEPS=false
SKIP_MIGRATE=false
MODE="full"

for arg in "$@"; do
    case "$arg" in
        --skip-deps)    SKIP_DEPS=true ;;
        --skip-migrate) SKIP_MIGRATE=true ;;
        --restart)      MODE="restart" ;;
        --help|-h)
            echo "用法: ./deploy_bare.sh [选项]"
            echo "  （不带参数）完整部署：依赖 -> alembic 迁移 -> token 检查 -> systemd 启动"
            echo "  --skip-deps     跳过依赖安装（已装过时用）"
            echo "  --skip-migrate  跳过 wellflow alembic 迁移"
            echo "  --restart       只重启服务"
            exit 0 ;;
    esac
done

echo "=============================================="
echo "  裸机部署 wellflow-saas-backend"
echo "  repo root : ${REPO_ROOT}"
echo "  python    : ${PYTHON_BIN}"
echo "  port      : ${PORT}"
echo "=============================================="
cd "$REPO_ROOT"

# ---------- 0. 重启模式 ----------
if [ "$MODE" = "restart" ]; then
    echo "==> 重启 systemd 服务 ${SERVICE_NAME} ..."
    sudo systemctl restart "${SERVICE_NAME}"
    sleep 3
    curl -fsS "http://127.0.0.1:${PORT}/health" && echo && echo "✅ 服务已重启" || echo "⚠️ 健康检查未通过，看日志: journalctl -u ${SERVICE_NAME} -n 50"
    exit 0
fi

# ---------- 1. .env 检查 ----------
echo ""
echo "==> 1/5 检查 .env ..."
if [ ! -f "${REPO_ROOT}/.env" ]; then
    cp .env.example .env
    echo "  ⚠️ 已从 .env.example 复制 .env，请务必填写以下必填项后再启动："
    echo "     POSTGRES_PASSWORD（PostgreSQL 密码）"
    echo "     QIANCHUAN_SECRET（千川应用密钥）"
    echo "     DEEPSEEK_API_KEY（DeepSeek 密钥）"
    exit 1
fi
grep -q "POSTGRES_PASSWORD" "${REPO_ROOT}/.env" || { echo "  ❌ .env 缺少 POSTGRES_PASSWORD"; exit 1; }
echo "  ✅ .env 就绪"

# ---------- 2. 依赖安装 ----------
if [ "$SKIP_DEPS" = false ]; then
    echo ""
    echo "==> 2/5 安装依赖 ..."
    if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
        echo "  ❌ 未找到 ${PYTHON_BIN}，请先安装 Python 3.12（含 venv）"
        echo "     Ubuntu/Debian: sudo apt install python3.12 python3.12-venv"
        echo "     CentOS/Rocky:  sudo dnf install python3.12 python3.12-pip"
        exit 1
    fi
    if [ ! -d "${VENV_DIR}" ]; then
        "${PYTHON_BIN}" -m venv "${VENV_DIR}"
        echo "  ✅ 创建虚拟环境 ${VENV_DIR}"
    fi
    "${VENV_DIR}/bin/pip" install --upgrade pip -q
    "${VENV_DIR}/bin/pip" install -r requirements.txt -r wellflow/requirements.txt -q
    echo "  ✅ 依赖安装完成（根 + wellflow 双 requirements）"
else
    echo ""
    echo "==> 2/5 跳过依赖安装（--skip-deps）"
fi

# ---------- 3. wellflow 数据库迁移 ----------
if [ "$SKIP_MIGRATE" = false ]; then
    echo ""
    echo "==> 3/5 执行 wellflow alembic 迁移 ..."
    if cd "${REPO_ROOT}/wellflow" && PYTHONPATH="${REPO_ROOT}" "${VENV_DIR}/bin/alembic" upgrade head; then
        echo "  ✅ alembic upgrade head 完成"
    else
        echo "  ⚠️ alembic 迁移失败，请确认 PostgreSQL 已启动且 .env 的 DATABASE_URL 正确"
        echo "     （若 PG 还没建 wellflow 库：sudo -u postgres createdb wellflow）"
        echo "     可使用 --skip-migrate 跳过继续"
    fi
    cd "$REPO_ROOT"
else
    echo ""
    echo "==> 3/5 跳过 alembic（--skip-migrate）"
fi

# ---------- 4. token_store.db 检查 ----------
echo ""
echo "==> 4/5 检查广告系统授权库 ad/token_store.db ..."
TOKEN_DB="${REPO_ROOT}/ad/token_store.db"
if [ -f "${TOKEN_DB}" ]; then
    echo "  ✅ 已找到 token_store.db（广告系统授权凭据已就位）"
else
    echo "  ⚠️ 服务器上没有 ad/token_store.db（该文件在 git 中已忽略，不会随代码同步）"
    echo "     两种处理方式任选其一："
    echo "     【方式A · 复制本机授权】开发机执行（先停本机服务）："
    echo "         scp ad/token_store.db 用户@服务器:${TOKEN_DB}"
    echo "     【方式B · 服务器重新授权】启动服务后访问 /api/token/fetch 用 auth_code 授权"
    echo "        （服务器 .env 的 APP_ID / QIANCHUAN_SECRET 必须与本机一致）"
fi

# ---------- 5. systemd 服务 ----------
echo ""
echo "==> 5/5 配置 systemd 服务 ..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ ! -f "${SERVICE_FILE}" ]; then
    echo "  创建 ${SERVICE_FILE} ..."
    sudo tee "${SERVICE_FILE}" > /dev/null <<EOF
[Unit]
Description=WellFlow SaaS Backend (ad + wellflow)
After=network.target postgresql.service

[Service]
Type=simple
WorkingDirectory=${REPO_ROOT}
Environment=PYTHONPATH=${REPO_ROOT}
ExecStart=${VENV_DIR}/bin/python -m uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1
Restart=always
RestartSec=5
User=$(whoami)

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}"
    echo "  ✅ 已创建并启用 systemd 服务（--workers 1，SQLite 单进程要求）"
else
    echo "  服务文件已存在，跳过创建"
fi

echo ""
echo "==> 启动服务 ..."
sudo systemctl restart "${SERVICE_NAME}"
sleep 5

echo ""
echo "==> 健康检查 http://127.0.0.1:${PORT}/health ..."
for _ in $(seq 1 15); do
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo ""
        echo "=============================================="
        echo "  ✅ 裸机部署完成"
        curl -s "http://127.0.0.1:${PORT}/health" | python3 -m json.tool 2>/dev/null || curl -s "http://127.0.0.1:${PORT}/health"
        echo ""
        echo "  📄 投放台页面:  http://服务器IP:${PORT}/"
        echo "  📄 API 文档  :  http://服务器IP:${PORT}/docs"
        echo "  📄 服务日志  :  journalctl -u ${SERVICE_NAME} -f"
        echo "=============================================="
        exit 0
    fi
    sleep 2
done

echo "❌ 健康检查超时，最近日志："
sudo journalctl -u "${SERVICE_NAME}" -n 50 --no-pager
exit 1
