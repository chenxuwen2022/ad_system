FROM python:3.12-slim

WORKDIR /app

# psycopg2-binary 自带 libpq，Pillow 等均有预编译 wheel，slim 镜像足够
#
# 国内服务器改用阿里云PyPI镜像，提升下载稳定性，减少断连重试
COPY requirements.txt .
COPY wellflow/requirements.txt wellflow/requirements.txt

# 新增：升级 pip setuptools wheel，解决旧pip无法找到新版psycopg2-binary的问题
RUN python -m pip install --upgrade pip setuptools wheel \
    -i https://pypi.mirrors.ustc.edu.cn/simple \
    --root-user-action=ignore

RUN pip install --no-cache-dir \
    --timeout 120 \
    --retries 2 \
    -i https://pypi.mirrors.ustc.edu.cn/simple \
    --root-user-action=ignore \
    -r requirements.txt -r wellflow/requirements.txt

# 源码：ad/ 广告投放子系统 + wellflow/ 商拍子系统 + 根目录 .env（config.py 从 /app/.env 读取）
COPY ad/ ad/
COPY main.py ./
COPY wellflow/ wellflow/
# main.py 会挂载 /static，目录必须存在；内容为运行时生成，不打进镜像
RUN mkdir -p static
COPY .env .

EXPOSE 8000

# 启动前先跑 Alembic 迁移，再起 uvicorn（统一入口 main.py 会同时挂载广告投放 + WellFlow）
# --workers 4 多进程并发；--loop uvloop 用 C 实现的 event loop 提速
CMD ["sh", "-c", "cd wellflow && PYTHONPATH=/app alembic upgrade head && cd /app && PYTHONPATH=/app uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --loop uvloop"]
