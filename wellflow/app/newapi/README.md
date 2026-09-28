# New API 连接模块

WellFlow 的 New API 连接、模型目录和模型池集中在此目录，各文件只有一份实现：

| 文件 | 职责 |
| --- | --- |
| `client_factory.py` | 读取连接配置、规范化模型名、创建客户端、设置客户端独立重试次数 |
| `gateway.py` | 文本 / 多模态的完整响应和 SSE 流式调用，以及 LangChain 适配 |
| `image_client.py` | 生图请求：`/images/generations`、`/images/edits`、`/responses` |
| `catalog.py` | 模型目录数据结构、能力识别、`/models` 和 `/api/channel/{id}` 查询 |
| `pool.py` | 模型轮询、熔断、失败切换、任务级缓存和清理 |
| `channel_audit.py` | 按请求 ID 查询 `/api/log/`，记录实际使用的渠道 |

## 调用入口

- 单个模型：直接调用 `newapi.client_factory.get_llm_client`。
- 模型目录：`newapi.catalog.fetch_model_options`，HTTP 路由和模型池直接复用。
- 模型池：`newapi.pool.get_model_pool`，任务预加载和清理也在同一模块。

`llm/base.py` 定义通用客户端契约、返回值和异常。意图识别、生图业务编排和排队等上层逻辑仍在 `llm` 中。
`api/model_options.py` 仅负责 HTTP 参数和响应包装。

WellFlow 与广告模块均直接使用 `newapi.pool`，共用同一份模型池实现和缓存。
原先两份 `model_options.py` 已合并到 `catalog.py`，New API 工厂使用 `client_factory.py`，避免与通用工厂重名。

## 后续增加其他连接方式

真正接入其他连接方式时，再根据需求设计统一选择入口；目前直接使用 New API 工厂。
如果新连接也需要模型目录和模型池，再按实际需求增加对应实现或提取共享接口。

当前默认仍是 New API，配置继续使用 `config.py` 中现有的 `NEWAPI_*` 和模型相关设置。
请求参数和超时保持不变。模型池创建客户端时传入 `max_retries=0`，自行负责失败切换；独立客户端保留默认重试次数，不再修改全局类属性。

## 验证

在仓库根目录执行：

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s wellflow/tests -p test_newapi.py -v
```

New API 测试使用模拟 HTTP 响应，无需请求真实模型。

## 依赖方向

- 单模型调用：业务 → `client_factory` → `gateway`。
- 模型池调用：业务 → `pool` → `catalog`（加载目录） / `client_factory`（创建客户端） → `gateway`。
- 生图：`gateway` 继承 `image_client` 的生图实现，按模型选择端点；生图模块不反向引用工厂。
- `llm/base.py`、配置和渠道审计是底层依赖，不回调上层工厂或模型池。

保留生图排队、限流和端点选择各自的职责；它们承担实际行为，并非单纯的函数转发。

## 统一日志格式

终端默认输出可读文本：

```text
[2026-09-24 16:53:33.258] [INFO] [newapi网关] [trace:77ff6e92258e] 开始调用 chat/completions | conv_id=45fce77f-ca6d-4a62-b5ae-2986a7a86cc3 | task=2e51b74d26a3 | sku=7 | model=deepseek-v4-flash | page=对话
```

使用进程环境变量 `WELLFLOW_LOG_FORMAT=json` 可切换为单行 JSON；默认是 `console`。两种输出使用同一结构化记录，JSON 和终端均展示完整对话 ID，不截断。当前不额外创建日志文件，也不重复输出两份。

```json
{"ts":"2026-09-24T16:53:33.258+08:00","biz":"newapi网关","trace_id":"77ff6e92258e","parent_trace_id":null,"conv_id":"45fce77f-ca6d-4a62-b5ae-2986a7a86cc3","task_id":"2e51b74d26a3","sku":7,"model":"deepseek-v4-flash","api":"chat/completions","action":"开始调用","page":"对话"}
```

### ID 的来源和含义

原 `operation_id` 是日志装饰器在业务操作开始时通过 `uuid4().hex[:12]` 生成的临时关联编号，不是数据库主键、不落业务表，也不是上游请求 ID。原 `call_id` 同样由日志层生成，标识一次模型调用。

现在统一使用 `trace_id`：每次业务操作和每次模型调用各自生成一个 trace；子操作的 `parent_trace_id` 指向父业务操作的 trace。调用开始、重试、完成或失败沿用同一个 trace。根操作在 JSON 中保留 `parent_trace_id: null`；没有 trace 的普通日志省略 trace 字段。终端仅显示当前 trace，父子关系保留在 JSON 中。

### 字段

- `ts`：带时区的毫秒时间；终端显示本地日期和时间。
- `biz`：当前模块，如意图识别、node1产品报告、node2商拍方案、newapi网关。
- `conv_id` / `task_id` / `sku`：真实业务标识，在调用上下文提供时记录。
- `model` / `api` / `action`：所选模型、接口、动作。
- `page`：页面功能；网关等共享模块继承对话、sku商品库、模特库或穿搭库的页面上下文。
- 耗时、重试、HTTP 状态、上游 `request_id`、错误原因和堆栈等诊断字段在有值时保留。
- 除根 trace 的 `parent_trace_id` 外，缺失字段省略；有效的 0 和 false 保留。

终端级别为 INFO / WARN / ERROR。错误堆栈的换行被转义，每条日志保持一行。“调用完成”只表示客户端正常返回，业务解析仍可能失败。

新增日志使用 `log_event` / `log_message`。页面入口使用 `@page_context`；业务操作使用 `@business_operation`。上下文通过异步子任务和 `asyncio.to_thread` 传递，不发送给模型上游。

`ad/` 和应用 root logger 保持原样。数据库迁移仅通过 `wellflow/alembic.ini` 使用本格式。

## 资产库生图模型参数

- 模特生成与微调的 `generate_model` 为必填，空白值返回 422；不再提供默认模型。
- 穿搭 `/outfit/ai-extract` 的 JSON 必须提供 `image_model`，后台每件抠图均使用同一个选定模型，不自动换模型。
- 穿搭 `/outfit/generate` 在 `mode=auto` 时必须提供 `image_model`；`mode=items` 是本地平铺合成，不要求生图模型。
- 图片客户端工厂拒绝缺失或空白模型，文本识别和自动打标仍走其文本/VLM 调用链。
- 当前项目未找到 `/scene/ai-extract` 的后端实现，场景选择模型的后端契约尚待接入；前端已按 `image_model` 提交选择值。
