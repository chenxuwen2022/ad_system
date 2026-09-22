from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent


class Settings(BaseSettings):
    # ------------------------------------------------------------------
    # 网关 —— 统一走 new-api 中转网关（局域网 192.168.110.254）
    # 业务层所有 VLM / 文本 LLM / 生图请求都经由 new-api，渠道分发由 new-api 后台配置。
    # ------------------------------------------------------------------
    newapi_base_url: str = "http://192.168.110.254/v1"
    newapi_api_key: str | None = None              # ← .env 提供
    newapi_admin_access_token: str | None = None    # 管理员面板 PAT，不是模型调用 API Key

    # ====== new-api 渠道 ID ======
    # LLM / VLM / text 模型统一走这个渠道拉列表 + 分发（Node1/Node2/Node3/意图识别/refine/outfit/mannequins 全部继承）
    # Node4 生图渠道单独配 node4_image_channel_id（语义不同，不要合并）
    llm_channel_id: int = 4

    # 模型池熔断参数（模型列表本身由 new-api /models?channel_id=llm_channel_id 动态获取）
    model_pool_fail_threshold: int = 2              # 连续 2 次失败熔断
    model_pool_fail_window: float = 10.0            # 失败统计窗口（秒）
    model_pool_cooldown: float = 30.0               # 熔断后冷却自动恢复（秒）


    # ====== Node1 ======
    llm_reasoning_effort: str = "low"   # Node1/Node2/Node3 统一 low（开启 thinking 但推理成本可控，逐 token 推 SSE）

    # ====== Node2 ======
    node2_reasoning_effort: str = "low"
    # Node2 默认生成 3 套风格迥异的候选方案，供 C2 阶段让用户挑选 1 套确认。
    # 最终进入 Node3 时只保留用户选中的那 1 套，围绕它生成 5 份差异化 prompt。
    node2_scheme_count_default: int = 3

    # ====== Node3 ======
    node3_reasoning_effort: str = "low"
    # C2 用户锁定 1 套方案后，Node3 围绕这一套生成几份差异化 prompt（每份 = 1 张生图）。
    # 新链路下固定为 5 —— C3 用户从 5 份里挑 1~5 份生图。
    node3_variants_per_scheme_default: int = 5
    node3_gen_concurrency: int = 10           # Node3 并行生图并发上限（Semaphore），越大越快但易触发 429

    # ====== Node4 ======
    node4_gen_concurrency: int = 3                 # Node4 生图队列 worker 数，对齐上游限流配额防 429
    node4_image_channel_id: int = 4                # new-api 生图渠道 ID
    node4_image_models_fallback: list[str] = [     # 动态拉失败时的硬编码兜底链
        "qwen-image-3.0",
        "gpt-image-2",
    ]
    llm_model_responses: str = "gpt-5.4-mini"  # responses 端点顶层 LLM（理解 prompt + 调用 image_generation tool）
    image_ratio_to_pixel_size_gpt: dict[str, str] = {
        "9:16": "1024x1536",   # 降级：用 3:4 近似 9:16
        "3:4": "1024x1536",
        "1:1": "1024x1024",
        "4:3": "1536x1024",
        "16:9": "1536x1024",   # 降级：用 4:3 近似 16:9
    }
    image_gen_quality: str = "medium"              # high | medium | low —— 生图质量/速度杠杆
    image_gen_input_fidelity: str = "low"          # high | low —— edit 模式下对参考图的保真强度
    image_gen_detail: str = "low"                  # high | low | auto —— input_image block 的 detail
    image_gen_proxy_url: str | None = None         # /v1/responses 是否走代理，None=直连（跳过 127.0.0.1:7890）
    image_gpt_edit_endpoint: str = "edits"         # edits（/v1/images/edits multipart）| responses（/v1/responses + image_generation tool）
    image_edit_quality: str = "high"               # /v1/images/edits multipart 生图质量（gpt-image 系列）

    # ====== 数据库 ======
    database_url: str = ""           # ← .env 提供
    database_url_async: str = ""     # ← .env 提供

    # ====== 其他 ======
    llm_timeout: float = 60.0                     # VLM / 文本 LLM 超时（秒）
    image_timeout: float = 180.0                   # 生图超时（秒）

    image_max_per_call: int = 3                    # 单次 VLM / 生图调用最多携带图片张数
    image_single_compress_threshold_mb: float = 1.5  # 单张图片超过此值触发渐进压缩（raw bytes）

    # ---- 图片压缩（Pillow）策略 ----
    pil_quality_start: int = 90                    # JPEG 质量起点（逐次降 quality 压缩）
    pil_quality_min: int = 50                      # JPEG 质量下限（低于此值画质不可接受）
    pil_max_side_fallback: int = 2800              # 极端兜底：quality=min 仍超限时收缩长边到这里

    image_ext_map: dict[str, str] = {
        "jpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif",
    }
    upload_dir: str = str(_PROJECT_ROOT / "uploads")   # 上传图片落盘目录（绝对路径）

    # ---- 上传 / 图库 / SKU 图片规则（uploads.py + products.py 共用，避免 20MB 到处写）----
    upload_allowed_mime_prefixes: tuple[str, ...] = ("image/",)  # uploads.py + products.py 共用
    upload_max_file_size_mb: int = 10              # 单张上传大小上限（MB，前后端一致）
    upload_max_files: int = 10                     # 通用上传单次最多几张
    sku_image_allowed_exts: set[str] = {"jpg", "jpeg", "png", "webp", "gif"}
    sku_image_min_count: int = 1
    sku_image_max_count: int = 9

    # ---- SSE / EventBus ----
    sse_db_fallback_interval_seconds: int = 30     # 事件驱动超过此时间没收到事件才查一次 DB
    sse_heartbeat_interval_seconds: int = 25       # SSE heartbeat 间隔（防代理掐连接）
    event_bus_queue_max_size: int = 64             # 每个 task_id 的队列容量（防慢消费者拖垮内存）
    event_bus_queue_idle_timeout_seconds: int = 600  # 队列闲置多久后自动清理（秒）

    # ---- graph 运行状态 ----
    graph_stale_threshold_seconds: int = 90        # checkpoint 年龄阈值，超过认为 graph 可能挂了

    # ====== 穿搭库（outfit）======
    outfit_extract_models: list[str] = [  # 抠图降级链(qwen 优先:2026-09-20 gpt 系网关无渠道,实测 qwen 唯一可用)
        "qwen-image-3.0",
        "gpt-image-2",
        "gpt-image-2.5-flare",
        "mai-image-2.5",
    ]
    outfit_max_items: int = 6              # VLM 单次识别最多提取几件单品
    outfit_extract_concurrency: int = 3    # 抠图并发上限（ThreadPoolExecutor）

    # ====== 场景库（scene）======
    scene_extract_models: list[str] = [  # 场景提取生图降级链(qwen 优先,同穿搭库口径)
        "qwen-image-3.0",
        "gpt-image-2",
        "gpt-image-2.5-flare",
        "mai-image-2.5",
    ]


    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @property
    def newapi_admin_base_url(self) -> str:
        """管理接口与 /v1 模型接口位于同一 New API 服务。"""
        return self.newapi_base_url.rstrip("/").removesuffix("/v1")

    @field_validator("image_gen_proxy_url", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("upload_dir", mode="before")
    @classmethod
    def _resolve_upload_dir(cls, v):
        """.env 里如果写相对路径（如 uploads），自动解析为 wellflow/uploads 绝对路径。"""
        if isinstance(v, str) and v.strip() and not Path(v).is_absolute():
            return str(_PROJECT_ROOT / v)
        return v

    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
