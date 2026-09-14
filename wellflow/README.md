# WellFlow 电商商拍平台后端

基于 **FastAPI + LangGraph + PostgreSQL** 的 AI 商拍任务编排系统。用户提交商拍需求后，系统通过 LangGraph 父图协调 **调研(Node1) → 规划(Node2) → 执行(Node3)** 三阶段 Agent 流水线，中间穿插人工确认点(C1/C2)，最终产出策划方案与生成图。

## 技术栈

| 类别 | 组件 |
|---|---|
| Web 框架 | FastAPI 0.115+ / Uvicorn |
| AI 编排 | LangGraph 0.2+（父图 + checkpointer） |
| 数据库 | PostgreSQL 16 + SQLAlchemy 2.x + Alembic |
| 异步驱动 | psycopg 3 (sync) + asyncpg (async) |
| Schema | Pydantic v2 |
| LLM 网关 | LaoZhang Gateway / Ofox Gateway（可扩展） |

## 目录结构

```
wellflow-saas-backend/
├── app/
│   ├── main.py              # FastAPI 应用入口 + lifespan（checkpointer 初始化）
│   ├── config.py            # .env 配置加载（pydantic-settings）
│   ├── database.py          # SQLAlchemy 同步引擎 + Session
│   ├── database_async.py    # 异步引擎（LangGraph checkpointer 用）
│   ├── errors.py            # 业务异常定义
│   ├── event_bus.py         # 进程内事件总线（SSE 推送后端）
│   │
│   ├── api/                 # 路由层
│   │   ├── tasks.py         # 任务 CRUD + 创建/恢复（SSE 直推）
│   │   └── utils.py         # 统一响应包装
│   ├── sse.py               # SSE 订阅端点（事件驱动 + DB 兜底）
│   │
│   ├── models/              # ORM 模型
│   │   ├── task_models.py   # 任务域：Task / TaskEvent / TaskErrorLog / TaskImage
│   │   └── __init__.py
│   ├── schemas/             # Pydantic 请求/响应
│   │   ├── task_schemas.py  # 任务域 Schema
│   │   ├── schemas.py       # 通用 ApiResponse / PageResponse
│   │   └── __init__.py
│   │
│   ├── repositories/        # 数据访问层
│   │   └── task_repo.py     # TaskRepo（封装 Task / TaskEvent / TaskImage CRUD）
│   │
│   ├── workflows/           # LangGraph 图定义
│   │   ├── parent_graph.py  # 父图（三阶段编排 + HITL 确认点）
│   │   ├── state.py         # 全局 State 定义
│   │   ├── node1_graph.py   # 调研子图
│   │   ├── node2_graph.py   # 规划子图
│   │   ├── node3_graph.py   # 执行子图
│   │   ├── confirmations.py # C1 / C2 确认点逻辑
│   │   └── finalize.py      # 收尾（落库生图成品 + 标记完成）
│   │
│   ├── nodes/               # Agent 节点实现
│   │   ├── research_agent.py   # Node1：市场调研 Agent
│   │   ├── input_analyzer.py   # 输入解析
│   │   ├── product_analyzer.py # 商品分析
│   │   ├── planning_agent.py   # Node2：方案规划 Agent
│   │   ├── prompt_composer.py  # 生图 Prompt 合成
│   │   └── human_review.py     # 人工确认节点
│   │
│   ├── tools/               # LangChain Tool（Node1 调研用）
│   │   ├── web_search.py
│   │   ├── platform_trend.py
│   │   └── competitor.py
│   │
│   ├── llm/                 # LLM 网关抽象
│   │   ├── base.py              # BaseLLMClient 协议
│   │   ├── factory.py           # 工厂（根据 settings 选网关）
│   │   ├── laozhang_gateway.py  # 老张网关
│   │   └── ofox_gateway.py      # Ofox 网关
│   │
│   ├── contracts/           # 跨节点数据契约（TypedDict）
│   │   ├── generation.py
│   │   └── product.py
│   │
│   ├── prompt/              # System Prompt 常量
│   └── utils/               # 工具函数（image_store 等）
│
├── migration/               # Alembic 迁移脚本
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── ee0f7959b15c_init_asset_library_tables.py    # ← 历史资产库（已 drop）
│       ├── 5efd8dd13e9a_add_task_domain_tables.py
│       ├── c3d4a1b2e5f7_add_task_image.py
│       ├── a7f2b8c9d0e1_drop_unused_generation_tables.py
│       └── f8a1b2c3d4e5_drop_asset_library_tables.py    # ← 最近：drop 6 张资产表
│
├── .env                     # 运行时配置（不入库）
├── alembic.ini              # Alembic 配置
├── Dockerfile               # 容器镜像（含 alembic upgrade head）
├── docker-compose.yml       # app + db (postgres:16)
├── deploy.sh                # 一键部署脚本（服务器上执行）
└── requirements.txt
```

## 快速开始

### 方式 A：Docker Compose（推荐）

```bash
cp .env.example .env     # 填入 POSTGRES_PASSWORD / LLM_API_KEY 等
docker compose up -d     # 自动构建 + 起服务 + alembic 迁移
curl http://localhost:8000/health
```

### 方式 B：本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # 至少配 DATABASE_URL
alembic upgrade head     # 首次建表

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

> ⚠️ LangGraph checkpointer 需要 PostgreSQL 连通；PG 不可用时应用会降级启动，仅图持久化功能不可用。

## 核心流程

```
POST /api/tasks          创建任务（SSE 直推，零延迟）
  │
  ▼
Node1 调研 ──→ C1 人工确认 ──→ Node2 规划 ──→ C2 人工确认 ──→ Node3 执行
  │                                              │                 │
  └─ market research                             └─ plan options   └─ generate images
     (web_search / trend / competitor)                               (生图网关)
  │
  ▼
done / failed
```

- **C1**：用户上传模特图 + 确认商品分析结论
- **C2**：用户从 Node2 产出的多个方案中选择并微调

## API 接口

### 任务 `/api/tasks`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 创建商拍任务（**SSE 直推**，一个请求走完整流程） |
| POST | `/api/tasks/{task_id}/resume` | 从 HITL 确认点恢复任务（C1 / C2 / review） |
| GET | `/api/tasks` | 分页列出任务 |
| GET | `/api/tasks/{task_id}` | 查询任务详情（含 phase / interrupt / node 输出） |
| DELETE | `/api/tasks/{task_id}` | 删除任务及所有关联数据 |

### SSE 订阅

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks/{task_id}/stream` | 订阅任务进度事件 |

SSE event 类型：`phase` / `interrupt` / `thinking_chunk` / `report_chunk` / `report_chunk_done` / `cost` / `done` / `error`

### 其他

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含 langgraph/checkpointer 状态） |
| GET | `/uploads/{path}` | 静态图片服务（uploads/ 目录） |

### 创建任务示例

```bash
curl -N -X POST "http://localhost:8000/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "夏季轻薄棉麻衬衫商拍",
    "platform": "taobao",
    "image_type": "ad",
    "marketing_goal": "acquisition"
  }'
```

返回是一个 `StreamingResponse`（SSE），第一个 event 就是 `task_id`，后续实时推送 phase 变化、interrupt（需要人工确认时）、done/error。

## 数据库表

当前 4 张：

| 表名 | 说明 |
|---|---|
| `task` | 任务主表（phase / request_json / interrupt_json） |
| `task_event` | 任务事件审计（phase_change / llm_call / cost 等） |
| `task_error_log` | 任务错误日志（code / retryable / details） |
| `task_image` | 任务关联图片（`model` = C1 上传模特图，`output` = 生图成品） |

已废弃（`f8a1b2c3d4e5` 迁移已 drop）：`model_asset` / `model_asset_tag` / `outfit_asset` / `outfit_asset_tag` / `background_asset` / `background_asset_tag`

查看当前表结构：

```bash
docker compose exec db psql -U postgres -d wellflow -c "\dt"
```

## 部署

服务器上一条命令：

```bash
git pull
./deploy.sh
```

脚本内部做的事：校验 `.env` → `docker compose build` → `docker compose up -d`。app 容器启动时 CMD 会自动执行 `alembic upgrade head` 再启 uvicorn。

## 注意事项

- `.env` 包含敏感信息，已加入 `.gitignore`
- LangGraph checkpointer 连接失败时应用会降级启动（图持久化不可用，但 API 正常）
- `uploads/` 目录通过 docker volume 挂载到宿主机 `/data/wellflow/uploads`，便于备份
- 修改模型后务必生成新迁移：`alembic revision --autogenerate -m "描述"`
