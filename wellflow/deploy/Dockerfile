FROM python:3.12-slim

WORKDIR /app

# psycopg2-binary 自带 libpq，Pillow 等均有预编译 wheel，slim 镜像足够
#
# 国内服务器访问 PyPI 官方源不稳定，改用清华镜像源（可按需换成阿里云/中科大）
COPY wellflow/requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 源码：wellflow/ 整个子目录 + 根目录 .env（config.py 从 /app/.env 读取）
COPY wellflow/ wellflow/
COPY .env .

EXPOSE 8000

# 启动前先跑 Alembic 迁移，再起 uvicorn
# alembic.ini 在 wellflow/ 下，且 CWD 需要 wellflow/ 以便 `from wellflow.app import ...` 解析
CMD ["sh", "-c", "cd wellflow && alembic upgrade head && uvicorn wellflow.app.main:app --host 0.0.0.0 --port 8000"]
