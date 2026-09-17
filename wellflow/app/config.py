from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent


class Settings(BaseSettings):
    # new-api 中转网关 — 所有 LLM/VLM/生图请求统一走这里
    newapi_base_url: str = "http://192.168.110.254/v1"
    newapi_api_key: str | None = None              # ← .env 提供

    # 通用 VLM 模型轮询池 —— Node1/2/3 + 意图识别 + 模特打标 全部走这里
    model_pool_domestic_models: list[str] = [
        "volcengine/doubao-seed-1-6-flash",
        "deepseek/deepseek-v4.1-flash",
        "qwen/qwen3.8-flash",
        "z-ai/glm-5.3-flash",
        "deepseek/deepseek-v4-flash-0731",
    ]
    model_pool_overseas_fallback: str = "google/gemini-3.7-flash"
    model_pool_fail_threshold: int = 2              # 连续 2 次失败熔断
    model_pool_fail_window: float = 10.0            # 失败统计窗口（秒）
    model_pool_cooldown: float = 30.0               # 熔断后冷却自动恢复（秒）


    # ====== Node1 ======
    llm_reasoning_effort: str | None = "medium"   # Node1 商品识别需要深度思考
    http_proxy_url: str | None = "http://127.0.0.1:7890"  # Node1 调研 tools（web_search / competitor / trend）走代理

    # ====== Node2 ======
    node2_reasoning_effort: str | None = "none"   # Node2 生成方案，关 Deep Thinking 但走流式
    node2_prompt_count_default: int = 3       # Node2 生成提示词数量（默认 3，前端 C1 可覆盖）

    # ====== Node3 ======
    node3_reasoning_effort: str | None = "none"   # Node3 生成生图 prompt，关 Deep Thinking 但走流式
    node3_images_per_prompt_default: int = 1  # 每个提示词生成几张图（C2 interrupt 前端选择，默认 1）
    node3_gen_concurrency: int = 10           # Node3 并行生图并发上限（Semaphore），越大越快但易触发 429

    # ====== Node4 ======
    llm_model_image: str = "openai/gpt-image-2"    # Node4 图像生成
    llm_model_responses: str = "openai/gpt-5.4-mini"  # responses 端点顶层 LLM（理解 prompt + 调用 image_generation tool）
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

    llm_model_text: str = "qwen/qwen-turbo"        # research_agent 纯文本调研（LangGraph tool-calling）

    image_max_per_call: int = 3                    # 单次 VLM / 生图调用最多携带图片张数
    image_single_compress_threshold_mb: float = 1.5  # 单张图片超过此值触发渐进压缩（raw bytes）

    image_ext_map: dict[str, str] = {
        "jpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif",
    }
    upload_dir: str = str(_PROJECT_ROOT / "uploads")   # 上传图片落盘目录（绝对路径）


    # ------------------------------------------------------------------
    # Validators
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
        env_file=str(_REPO_ROOT / ".env"),
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
