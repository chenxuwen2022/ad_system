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
# 容器内 wellflow/ 位于 /app/wellflow，从 /app/wellflow 内部找不到 wellflow 包
# （会变成 /app/wellflow/wellflow/ 不存在），需要 PYTHONPATH=/app 让 Python 从 /app 找 wellflow/
# alembic.ini 已有 prepend_sys_path=.. 做了同样的事，这里显式加 PYTHONPATH 统一保证
CMD ["sh", "-c", "cd wellflow && PYTHONPATH=/app alembic upgrade head && PYTHONPATH=/app uvicorn wellflow.app.main:app --host 0.0.0.0 --port 8000"]
