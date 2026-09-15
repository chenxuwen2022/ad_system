from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 根目录（WellFlow 的上层，也是整个仓库根目录）
_REPO_ROOT = _PROJECT_ROOT.parent


class Settings(BaseSettings):
    # ------------------------------------------------------------------
    # 数据库（密码敏感，放 .env 覆盖）
    # ------------------------------------------------------------------
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/wellflow"
    database_url_async: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/wellflow"

    # ------------------------------------------------------------------
    # LLM / VLM / Creative — 网关与模型分配
    #   所有请求统一走 new-api 中转网关（局域网），由 new-api 按模型名路由到真实后端
    #   API key 敏感 → .env 覆盖；base_url / 模型名 → 固定在 config.py
    # ------------------------------------------------------------------
    llm_timeout: float = 60.0                # VLM / 文本 LLM 超时（秒）
    image_timeout: float = 180.0             # 生图超时（秒）——大 body 上传 + 生图处理比 VLM 慢得多

    # 模型名（固定分配，按需修改此处）
    llm_model_vlm: str = "google/gemini-3.7-flash"                 # Node 1 商品识别 + Node 2 商拍策划（多模态 VLM）
    llm_model_image: str = "openai/gpt-image-2"    # Node 3 图像生成
    llm_model_responses: str = "openai/gpt-5.4-mini"  # responses 端点的顶层 LLM（理解 prompt + 调用 image_generation tool）

    # ------------------------------------------------------------------
    # 图像生成协议适配（不同模型的 payload 差异）
    #   ofox 网关对不同系列模型走不同协议层，需要在客户端侧适配
    # ------------------------------------------------------------------

    # 走 Gemini 原生协议层的模型 — ofox 不认 n / 不认 reference_images
    # 目前只保留 GPT Image 系列，暂时置空
    image_gemini_models: list[str] = [
        # "google/gemini-3-pro-image",
        # "google/gemini-3.1-flash-image",
        # "google/gemini-3.1-flash-lite-image",
    ]

    # Lite 版 Gemini — size 只能用 aspect ratio（不能用像素格式）
    image_lite_models: list[str] = [
        # "google/gemini-3.1-flash-lite-image",
    ]

    # Lite 版需要像素格式 → aspect ratio 降级（找不到则丢弃 size）
    # Responses 端点只认这 3 种固定格式
    image_pixel_to_ratio: dict[str, str] = {
        "1024x1024": "1:1",
        "1536x1024": "4:3",
        "1024x1536": "3:4",
    }

    # 前端 ratio 标签（clean 后）→ 后端传给 LLM 的像素 size（Node 3 用）
    # 目前只有 GPT Image 系列走 image_ratio_to_pixel_size_gpt，这个旧映射暂时注释
    image_ratio_to_pixel_size: dict[str, str] = {
        # "9:16": "720x1280",
        # "3:4": "768x1024",
        # "1:1": "1024x1024",
        # "4:3": "1024x768",
        # "16:9": "1280x720",
    }

    # Responses 端点 /v1/responses 专用分辨率（只认这 3 种固定格式 + auto）
    # 9:16 / 16:9 降级到最接近的格式
    image_ratio_to_pixel_size_gpt: dict[str, str] = {
        "9:16": "1024x1536",   # 降级：用 3:4 近似 9:16
        "3:4": "1024x1536",
        "1:1": "1024x1024",
        "4:3": "1536x1024",
        "16:9": "1536x1024",   # 降级：用 4:3 近似 16:9
    }

    # 非 GPT 模型高分辨率映射（通义万相 / Gemini 等支持 2K+）
    image_ratio_to_pixel_size_hd: dict[str, str] = {
        "9:16": "1152x2048",
        "3:4": "1248x1664",
        "1:1": "2048x2048",
        "4:3": "1664x1248",
        "16:9": "2048x1152",
    }

    # 非 GPT /responses 模型列表（这些模型走 /v1/images/generations JSON，参考图通过 reference_images 传递）
    # GPT Image 系列（openai/gpt-image-*）单独走 /v1/images/edits multipart（通过 new-api 路由）
    image_non_gpt_models: list[str] = []


    # data URI MIME 扩展名 → 本地文件扩展名映射
    image_ext_map: dict[str, str] = {
        "jpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif",
    }

    # 上传图片落盘目录（绝对路径，固定为 wellflow/uploads/）—— state 只存文件路径，不再塞 data URI
    upload_dir: str = str(_PROJECT_ROOT / "uploads")

    # new-api 中转网关 — 所有 LLM/VLM/生图请求统一走这里，由 new-api 按模型名路由到真实后端
    newapi_base_url: str = "http://192.168.110.254/v1"
    newapi_api_key: str | None = None         # ← .env 提供

    # ------------------------------------------------------------------
    # Node1 调研阶段
    #   node1_research_use_tools:   True=调 tools（web_search / competitor / trend）
    #                               False=只让 LLM 凭世界知识写调研报告
    # ------------------------------------------------------------------
    node1_research_use_tools: bool = True
    http_proxy_url: str | None = "http://127.0.0.1:7890"
    platform_open: str = "taobao"             # taobao | xiaohongshu | douyin

    # ------------------------------------------------------------------
    # 图片上传 + 压缩统一配置（Node1 / Node2 / Node3 共用）
    # ------------------------------------------------------------------
    image_max_per_call: int = 3                     # 单次 VLM / 生图调用最多携带图片张数
    image_single_compress_threshold_mb: float = 5.0  # 单张图片超过此值才压缩（raw bytes，非 base64），小图原封不动

    # ------------------------------------------------------------------
    # Node2 / Node3 数量控制
    # ------------------------------------------------------------------
    node2_prompt_count_default: int = 3       # Node2 生成提示词数量（默认 3，前端 C1 可覆盖）
    node3_images_per_prompt_default: int = 1  # 每个提示词生成几张图（C2 interrupt 前端选择，默认 1）

    # ------------------------------------------------------------------
    # 生图 API 参数控制（/v1/responses image_generation tool）
    # 调高质量 → 慢；调低 → 快。默认 medium + low 保证速度
    # ------------------------------------------------------------------
    image_gen_quality: str = "medium"          # high | medium | low —— 生图质量/速度杠杆
    image_gen_input_fidelity: str = "low"      # high | low —— edit 模式下对参考图的保真强度
    image_gen_detail: str = "low"              # high | low | auto —— input_image block 的 detail
    image_gen_proxy_url: str | None = None     # ofox /v1/responses 是否走代理，None=直连（跳过 127.0.0.1:7890）
    # GPT Image（含 gpt-image-2）图生图端点选择：edits | responses
    #   edits      → /v1/images/edits multipart（gpt-image-2 原生编辑端点，参考图作为多个同名 image 字段）
    #   responses  → /v1/responses + image_generation tool (action=edit, input_image blocks)
    image_gpt_edit_endpoint: str = "edits"
    image_edit_quality: str = "high"           # /v1/images/edits multipart 生图质量（gpt-image 系列）
    node3_gen_concurrency: int = 10              # Node3 并行生图并发上限（Semaphore），越大越快但易触发 429

    # ------------------------------------------------------------------
    # LLM 深度思考 / Reasoning 控制
    # 可选值：None（用模型默认）/ "none" / "low" / "medium" / "high"
    # Gemini 3.1+ 等推理模型支持，通过 OpenAI 协议 reasoning_effort 参数透传
    # ------------------------------------------------------------------
    llm_reasoning_effort: str | None = "medium"     # Node1 商品识别需要深度思考
    node2_reasoning_effort: str | None = "none"      # Node2 生成 prompt，关 Deep Thinking 但走流式

    # ------------------------------------------------------------------
    # LangGraph 运行限制
    # ------------------------------------------------------------------
    parent_recursion_limit: int = 15
    node1_recursion_limit: int = 20
    node2_recursion_limit: int = 10

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    app_env: str = "development"
    cors_origins: list[str] = ["*"]

    # ------------------------------------------------------------------
    # 规范化：环境变量传空字符串时 Pydantic 不会自动转 None，
    # httpx 收到 proxy="" 会抛 Unknown scheme for proxy URL URL('')
    # 这里统一把空串/全空白 → None
    # ------------------------------------------------------------------
    @field_validator("http_proxy_url", "image_gen_proxy_url", mode="before")
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
        env_file=str(_REPO_ROOT / ".env"),  # 根目录唯一一份 .env，用绝对路径
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
