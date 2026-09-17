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

    # ── Node 级别模型分配（核心业务节点，可独立调优）────────────────────────
    #   Node1 → Google gemini-3.8-flash（商品识别，多模态 VLM）
    #   Node2 → OpenAI gpt-5.6-sol（商拍方案策划，多模态 VLM + JSON 输出）
    #   Node3 → Google gemini-3.7-flash（生图 prompt 生成，多模态 VLM + JSON 输出）
    # factory.get_llm_client(..., node_name="nodeX") 优先读取这些常量
    llm_model_node1: str = "google/gemini-3.8-flash"
    llm_model_node2: str = "openai/gpt-5.6-sol"
    llm_model_node3: str = "google/gemini-3.7-flash"

    # ── Role 级别默认模型（兜底；未指定 node_name 时使用）─────────────────────
    #   image  → Node4 图像生成（模特库 + LangGraph 主工作流）
    #   text   → research_agent 纯文本调研（LangGraph tool-calling）
    llm_model_image: str = "openai/gpt-image-2"
    llm_model_text: str = "qwen/qwen-turbo"

    # responses 端点顶层 LLM（理解 prompt + 调用 image_generation tool；base.py 动态 getattr）
    llm_model_responses: str = "openai/gpt-5.4-mini"

    # /api/chat 意图识别模型 —— 轻量快模型足够（qwen-turbo 稳定 500ms 内）
    llm_model_chat: str = "qwen/qwen-turbo"

    # Responses 端点 /v1/responses 专用分辨率（只认这 3 种固定格式 + auto）
    # 9:16 / 16:9 降级到最接近的格式
    image_ratio_to_pixel_size_gpt: dict[str, str] = {
        "9:16": "1024x1536",   # 降级：用 3:4 近似 9:16
        "3:4": "1024x1536",
        "1:1": "1024x1024",
        "4:3": "1536x1024",
        "16:9": "1536x1024",   # 降级：用 4:3 近似 16:9
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
    http_proxy_url: str | None = "http://127.0.0.1:7890"  # Node1 调研 tools（web_search / competitor / trend）走代理

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
    node2_reasoning_effort: str | None = "none"      # Node2 生成方案，关 Deep Thinking 但走流式
    node3_reasoning_effort: str | None = "none"      # Node3 生成生图 prompt，关 Deep Thinking 但走流式

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
