"""模特库 API（参考素材 > 模特库）。"""

from __future__ import annotations

import asyncio
import json as json_mod
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File
from sqlalchemy.orm import Session

from wellflow.app.config import settings
from wellflow.app.database import get_db
from wellflow.app.repositories.mannequin_repo import MannequinRepo, MANNEQUIN_DIMENSION_GROUPS
from wellflow.app.schemas.asset_schemas import (
    MannequinCreateRequest, MannequinUpdateRequest,
    MannequinListItem, MannequinListResponse,
    MannequinDetailResponse, MannequinDimensionsResponse,
    MannequinTagIn,
    # 新增：创建流程交互端点的 schema
    UploadedImage,
    MannequinOptimizePromptRequest, MannequinOptimizePromptResponse,
    GeneratedImage, MannequinGenerateRequest, MannequinGenerateResponse,
    MannequinFineTuneRequest, MannequinFineTuneResponse,
    MannequinAutoTagRequest, MannequinAutoTagResponse,
)


router = APIRouter(prefix="/reference/mannequins", tags=["模特库"])


def _storage_uri_url(uri: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith("http"):
        return uri
    return "/" + uri.lstrip("/")


def _tags_to_grouped(tags_rows) -> list[MannequinTagIn]:
    """把 repo 返回的扁平 tag 列表按 group_key+dim_key 聚合。"""
    grouped: dict[tuple[str, str], list[str]] = {}
    for t in tags_rows:
        key = (t["group_key"], t["dim_key"])
        grouped.setdefault(key, []).append(t["tag_value"])
    result = []
    for (gk, dk), vals in grouped.items():
        result.append(MannequinTagIn(group_key=gk, dim_key=dk, tag_values=vals))
    return result


# ============================================================================
# 维度枚举（前端下拉菜单）
# ============================================================================

@router.get("/dimensions", response_model=MannequinDimensionsResponse, summary="获取全部维度选项（前端筛选下拉菜单用）")
def get_dimensions():
    """返回硬编码的维度分组和可选值。"""
    return MannequinDimensionsResponse(groups=MANNEQUIN_DIMENSION_GROUPS)


# ============================================================================
# CRUD
# ============================================================================

@router.get("", response_model=MannequinListResponse, summary="列出模特（支持 scope / q 搜索 / 多维筛选）")
def list_mannequins(
    scope: str | None = Query(default=None, description="归属范围：official（官方公共模特）/ mine（个人私有）/ 不传表示全部"),
    q: str | None = Query(default=None, description="关键词或自然语言描述，匹配模特名称/英文名/编号/描述"),
    dims: str | None = Query(default=None, description="维度标签筛选 JSON，格式: {\"性别\":[\"女\"],\"模特风格\":[\"极简\"]}。可用维度键和值见 /reference/mannequins/dimensions 接口"),
    page: int = Query(1, description="当前页码，从 1 开始"),
    page_size: int = Query(20, description="每页条数，默认 20"),
    db: Session = Depends(get_db),
):
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    parsed_dims: dict[str, list[str]] | None = None
    if dims:
        try:
            raw = json_mod.loads(dims)
            parsed_dims = {k: [str(x) for x in v] for k, v in raw.items() if v}
        except Exception:
            raise HTTPException(400, "dims 参数必须是合法 JSON")

    repo = MannequinRepo(db)
    items, total = repo.list(scope=scope, q=q, dims=parsed_dims, page=page, page_size=page_size)

    out_items = []
    for m in items:
        tag_rows = repo.list_tags(m.id)
        tag_summary = []
        for t in tag_rows:
            tag_summary.extend(t["tag_values"])
        # 前 5 个标签作为卡片展示
        out_items.append(MannequinListItem(
            id=m.id,
            mannequin_no=m.mannequin_no,
            name=m.name,
            en_name=m.en_name,
            scope=m.scope,
            origin=m.origin,
            status=m.status,
            cover_storage_uri=m.cover_storage_uri,
            tag_summary=tag_summary[:5],
            description=m.description,
            created_at=m.created_at.isoformat(),
            updated_at=m.updated_at.isoformat(),
        ))

    return MannequinListResponse(items=out_items, total=total, page=page, page_size=page_size)


@router.get("/{mannequin_id}", response_model=MannequinDetailResponse, summary="查询模特详情")
def get_mannequin(mannequin_id: int, db: Session = Depends(get_db)):
    repo = MannequinRepo(db)
    m = repo.get(mannequin_id)
    if not m:
        raise HTTPException(404, "模特不存在")
    tag_rows = repo.list_tags(mannequin_id)
    return MannequinDetailResponse(
        id=m.id,
        mannequin_no=m.mannequin_no,
        name=m.name,
        en_name=m.en_name,
        scope=m.scope,
        origin=m.origin,
        status=m.status,
        cover_storage_uri=m.cover_storage_uri,
        cover_url=_storage_uri_url(m.cover_storage_uri),
        description=m.description,
        generate_model=m.generate_model,
        generate_prompt=m.generate_prompt,
        tags=_tags_to_grouped(tag_rows),
        created_at=m.created_at.isoformat(),
        updated_at=m.updated_at.isoformat(),
    )


@router.post(
    "",
    response_model=MannequinDetailResponse,
    summary="确认入库（写 Mannequin + Tag + GenerateLog，路径A/B 最终都走到这里）",
    tags=["模特创建流程 · 入库端点"],
)
def create_mannequin(body: MannequinCreateRequest, db: Session = Depends(get_db)):
    """模特最终入库。

    **流程定位**：模特创建流程的 **终点**，只有用户点了「确认入库」才调用。

    **落库**：一次事务写三张表：
    - `mannequin`：主表（name, cover_storage_uri, description, origin 等）
    - `mannequin_tag`：N 条标签（group_key / dim_key / tag_values）
    - `mannequin_generate_log`：AI 生成上下文（路径 A 时自动跳过；路径 B 时首轮 + 微调各写一条）

    **路径 A vs B**：
    - **路径 A（只上传）**：`origin=upload`，AI 字段全为 None
    - **路径 B（AI 生成）**：`origin=ai_generate`，传 AI 字段；如果走了微调同时传 `fine_tune_from` + `fine_tune_prompt`
    """
    repo = MannequinRepo(db)
    try:
        m = repo.create(
            name=body.name,
            en_name=body.en_name,
            scope=body.scope,
            origin=body.origin,
            cover_storage_uri=body.cover_storage_uri,
            description=body.description,
            tags=[t.model_dump() for t in body.tags] if body.tags else None,
            # AI 生成上下文
            input_desc=body.input_desc,
            input_refs=body.input_refs,
            final_prompt=body.final_prompt,
            generate_model=body.generate_model,
            num_output=body.num_output,
            fine_tune_from=body.fine_tune_from,
            fine_tune_prompt=body.fine_tune_prompt,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"创建失败: {e}")

    tag_rows = repo.list_tags(m.id)
    return MannequinDetailResponse(
        id=m.id, mannequin_no=m.mannequin_no, name=m.name, en_name=m.en_name,
        scope=m.scope, origin=m.origin, status=m.status,
        cover_storage_uri=m.cover_storage_uri,
        cover_url=_storage_uri_url(m.cover_storage_uri),
        description=m.description,
        generate_model=m.generate_model, generate_prompt=m.generate_prompt,
        tags=_tags_to_grouped(tag_rows),
        created_at=m.created_at.isoformat(), updated_at=m.updated_at.isoformat(),
    )


@router.put("/{mannequin_id}", response_model=MannequinDetailResponse, summary="更新模特")
def update_mannequin(mannequin_id: int, body: MannequinUpdateRequest, db: Session = Depends(get_db)):
    repo = MannequinRepo(db)
    try:
        m = repo.update(
            mannequin_id,
            name=body.name,
            en_name=body.en_name,
            scope=body.scope,
            cover_storage_uri=body.cover_storage_uri,
            description=body.description,
            tags=[t.model_dump() for t in body.tags] if body.tags is not None else None,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))

    tag_rows = repo.list_tags(m.id)
    return MannequinDetailResponse(
        id=m.id, mannequin_no=m.mannequin_no, name=m.name, en_name=m.en_name,
        scope=m.scope, origin=m.origin, status=m.status,
        cover_storage_uri=m.cover_storage_uri,
        cover_url=_storage_uri_url(m.cover_storage_uri),
        description=m.description,
        generate_model=m.generate_model, generate_prompt=m.generate_prompt,
        tags=_tags_to_grouped(tag_rows),
        created_at=m.created_at.isoformat(), updated_at=m.updated_at.isoformat(),
    )


@router.delete("/{mannequin_id}", summary="删除模特（级联清理标签，生成日志保留）")
def delete_mannequin(mannequin_id: int, db: Session = Depends(get_db)):
    repo = MannequinRepo(db)
    ok = repo.delete(mannequin_id)
    if not ok:
        raise HTTPException(404, "模特不存在")
    db.commit()
    return {"deleted": True, "mannequin_id": mannequin_id}


# ============================================================================
# 模特创建流程 —— 交互端点（无状态，不落库，只操作文件 + 调 LLM）
# ============================================================================

def _short_session_id() -> str:
    """生成 8 字符的 session id（用于组织上传的临时文件目录）。"""
    return "mne_" + uuid.uuid4().hex[:8]


def _uris_to_data_uris(uris: list[str]) -> list[str]:
    """把存储路径/URI → data URI（给 LLM 调用用）。"""
    from wellflow.app.utils.image_store import paths_to_data_uris
    return paths_to_data_uris(uris)


# ───────────────────────────────────────────────────────────────────────────
# 0. 上传图片（只存文件不落库）
# ───────────────────────────────────────────────────────────────────────────

@router.post(
    "/upload",
    summary="0. 上传模特图片（只存文件不落库，返回 session_id + storage_uri 给后续端点用）",
    tags=["模特创建流程 · 交互端点"],
)
async def upload_mannequin_images(
    session_id: str | None = Query(
        default=None,
        description="前端自己生成的 session_id（任意唯一字符串）；不传则后端自动生成并返回",
    ),
    files: list[UploadFile] = File(
        default_factory=list,
        description="可一次多张；路径A的原图 或 路径B的参考图都走这个端点",
    ),
):
    """上传模特图片（路径 A 原图 / 路径 B 参考图通用）。

    **流程定位**：模特创建流程的 **第 0 步**，所有后续端点都依赖这里返回的 `session_id` 和 `storage_uri`。

    **落库策略**：**只落盘**，不写任何数据库记录。等「确认入库」时才一次性写 Mannequin + Tag + GenerateLog。

    **目录结构**：`uploads/{session_id}/m0.jpg, m1.webp, ...`
    """
    if not files:
        raise HTTPException(400, "至少要上传 1 张图片")

    sid = session_id or _short_session_id()
    raw_files = [(f.filename or "image", await f.read(), f.content_type) for f in files]

    from wellflow.app.utils.image_store import save_upload
    paths = save_upload(sid, raw_files, prefix="m")

    uploaded: list[UploadedImage] = []
    for i, p in enumerate(paths):
        uploaded.append(UploadedImage(
            storage_uri=p,
            url=_storage_uri_url(p),
            filename=files[i].filename or f"image{i}",
        ))

    return {
        "session_id": sid,
        "count": len(uploaded),
        "images": uploaded,
    }


# ───────────────────────────────────────────────────────────────────────────
# 1. 优化提示词（纯文本 LLM，可选）
# ───────────────────────────────────────────────────────────────────────────

@router.post(
    "/optimize-prompt",
    response_model=MannequinOptimizePromptResponse,
    summary="1. 优化提示词（可选，bailian/qwen-turbo 纯文本，把原始提示词整合成适合生图的英文 prompt）",
    tags=["模特创建流程 · 交互端点"],
)
async def optimize_prompt(body: MannequinOptimizePromptRequest):
    """把用户原始描述 + 维度标签整合成结构化、细节丰富的英文生图 prompt。

    **流程定位**：路径 B（AI 生成）的 **可选步骤**。点击前端「优化提示词」按钮才调用。
    如果用户不点这个按钮，可以跳过，直接用原始提示词调 `/generate`。

    **模型**：`qwen/qwen-turbo`（纯文本 chat.completions）。

    **返回**：优化后的英文 prompt（前端展示给用户，可以继续手动编辑后再传给 `/generate`）。
    """
    from wellflow.app.llm.factory import get_llm_client

    # 把标签转成可读的中文描述（给 LLM 理解）
    tag_lines: list[str] = []
    for t in body.tags:
        vals = " / ".join(t.tag_values)
        tag_lines.append(f"  - {t.group_key} > {t.dim_key}: {vals}")
    tags_text = "\n".join(tag_lines) if tag_lines else "（未选择任何维度标签）"

    system = (
        "你是一个专业的电商模特形象提示词优化专家。"
        "你的任务是把用户的原始描述 + 维度标签整合成一段结构化、细节丰富、"
        "适合 AI 图像生成模型（如 GPT Image 系列）的英文提示词。\n\n"
        "规则：\n"
        "1. 保留用户的核心意图，不要创造新的属性。\n"
        "2. 把维度标签自然融入描述中，注意描述的连贯性。\n"
        "3. 输出英文，直接给生图模型用，不要带解释性文字、不要带前缀/后缀。\n"
        "4. 如果用户原始描述中提到了具体的商品（如某件衬衫），请将其与模特描述融合，"
        "说明模特正在如何展示该商品。"
    )

    user_text = (
        f"原始描述：{body.raw_prompt}\n\n"
        f"维度标签：\n{tags_text}\n\n"
        f"参考图数量：{len(body.ref_uris)}张\n\n"
        f"请输出优化后的英文提示词："
    )

    model_name = settings.llm_model_text
    print(f"[mannequin/optimize-prompt] 📤 调用 {model_name} 优化提示词", flush=True)

    try:
        # 纯文本 → 用 chat，不需要 chat_with_images
        client = get_llm_client("vlm", model_override=model_name)
        resp = await client.chat(
            system=system,
            user=user_text,
            temperature=0.3,
        )
        final_prompt = (resp.content or "").strip()
        if not final_prompt:
            raise RuntimeError("LLM 返回空内容")

        print(f"[mannequin/optimize-prompt] ✅ 优化完成 ({len(final_prompt)} chars)", flush=True)
        return MannequinOptimizePromptResponse(final_prompt=final_prompt)

    except Exception as e:
        print(f"[mannequin/optimize-prompt] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"提示词优化失败: {e}")


# ───────────────────────────────────────────────────────────────────────────
# 2. 首轮批量生图（n 张，路径 B 必须步骤）
# ───────────────────────────────────────────────────────────────────────────

@router.post(
    "/generate",
    response_model=MannequinGenerateResponse,
    summary="2. 首轮批量生图（路径B必须，gpt-image-2 生成 n 张模特图并自动落盘）",
    tags=["模特创建流程 · 交互端点"],
)
async def generate_mannequin_images(body: MannequinGenerateRequest):
    """根据最终提示词 + 参考图，调用生图模型批量生成 n 张模特图。

    **流程定位**：路径 B（AI 生成）的 **必须步骤**。路径 A（只上传）跳过这个端点。

    **模型**：前端下拉选择 → 后端映射后走 `openai/gpt-image-2`（或其他前端选定的模型）。

    **落盘**：每张图自动保存到 `uploads/{session_id}/outputs/gen-01.png` 等，返回完整的
    `storage_uri` + `url` + `base64` 三联，后续 `/fine-tune` 和 `/auto-tag` 用 `storage_uri` 引用。

    **并发**：内部用 Semaphore（默认 10）限流，防止 new-api 429。

    **参考图**：路径 A 上传的图通过 `ref_uris` 数组传入，作为生图的视觉参考。
    """
    import asyncio as _asyncio
    from wellflow.app.llm.factory import get_llm_client
    from wellflow.app.utils.image_store import save_output_image

    # 前端直接传 new-api 可识别的短名（如 gpt-image-2），后端透传
    backend_model = body.generate_model.strip()
    client = get_llm_client("image", model_override=backend_model)

    # 参考图 → data URI（生图模型需要）
    ref_data_uris = _uris_to_data_uris(body.ref_uris) if body.ref_uris else None

    # 并发限流（防止 new-api 429）
    sem = _asyncio.Semaphore(settings.node3_gen_concurrency)

    print(f"[mannequin/generate] 📤 model={backend_model} n={body.num_output} "
          f"refs={len(ref_data_uris) if ref_data_uris else 0} size={body.size} "
          f"session={body.session_id}", flush=True)

    async def _one(i: int):
        async with sem:
            r = await client.generate_image(
                prompt=body.prompt,
                image_uris=ref_data_uris,
                size=body.size,
                n=1,
                response_format="b64_json",
            )
            img = r.all_images[0]
            # 落盘
            data_uri = f"data:image/png;base64,{img.b64_json}"
            storage_uri = save_output_image(body.session_id, f"gen-{i:02d}", data_uri)
            url = _storage_uri_url(storage_uri) if storage_uri else None
            return GeneratedImage(
                index=i,
                base64=img.b64_json,
                storage_uri=storage_uri,
                url=url,
                revised_prompt=getattr(img, "revised_prompt", None),
            )

    try:
        tasks = [_one(i + 1) for i in range(body.num_output)]
        results = await _asyncio.gather(*tasks, return_exceptions=True)

        images: list[GeneratedImage] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                print(f"[mannequin/generate] ⚠️ 第{i+1}张生成失败: {r}", flush=True)
                continue
            images.append(r)

        if not images:
            raise HTTPException(502, "全部生图失败")

        print(f"[mannequin/generate] ✅ 成功 {len(images)}/{body.num_output}", flush=True)
        return MannequinGenerateResponse(
            images=images,
            model=backend_model,
            final_prompt=body.prompt,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[mannequin/generate] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"生图失败: {e}")


# ───────────────────────────────────────────────────────────────────────────
# 3. 单张微调（可选，图生图）
# ───────────────────────────────────────────────────────────────────────────

@router.post(
    "/fine-tune",
    response_model=MannequinFineTuneResponse,
    summary="3. 单张微调（可选，图生图，对选中的一张模特图做局部/风格微调）",
    tags=["模特创建流程 · 交互端点"],
)
async def fine_tune_mannequin(body: MannequinFineTuneRequest):
    """对选中的一张模特图做图生图微调，保持身份一致性 + 应用微调描述。

    **流程定位**：路径 B（AI 生成）的 **可选步骤**。用户从首轮 n 张里选 1 张 + 输入微调词后调用。
    最终入库的图 = 微调结果（如果走了微调）or 选中的那张生成图（如果没走微调）。

    **模型**：同 `/generate`，前端选的生图模型。

    **原理**：把 `target_uri` 作为唯一参考图 + 微调提示词组合（保持身份 + 修改内容）传给模型。

    **落盘**：微调结果保存到 `uploads/{session_id}/outputs/fine-tuned.png`，返回三联。
    """
    from wellflow.app.llm.factory import get_llm_client
    from wellflow.app.utils.image_store import save_output_image

    # 前端直接传 new-api 可识别的短名（如 gpt-image-2），后端透传
    backend_model = body.generate_model.strip()
    client = get_llm_client("image", model_override=backend_model)

    # 目标图必须是 data URI
    target_data_uris = _uris_to_data_uris([body.target_uri])
    if not target_data_uris:
        raise HTTPException(400, f"无法读取 target_uri: {body.target_uri}")

    # 微调 prompt = 维持身份 + 修改内容
    identity = body.original_prompt or "保持模特面部特征、发型、整体气质和身份一致性"
    tune_prompt = f"{identity}, 修改内容：{body.tune_prompt}"

    print(f"[mannequin/fine-tune] 📤 model={backend_model} "
          f"target={body.target_uri[:40]}... session={body.session_id}", flush=True)

    try:
        r = await client.generate_image(
            prompt=tune_prompt,
            image_uris=target_data_uris,
            size=body.size,
            n=1,
            response_format="b64_json",
        )
        img = r.all_images[0]
        # 落盘
        data_uri = f"data:image/png;base64,{img.b64_json}"
        storage_uri = save_output_image(body.session_id, "fine-tuned", data_uri)
        url = _storage_uri_url(storage_uri) if storage_uri else None
        print(f"[mannequin/fine-tune] ✅ 成功 → {storage_uri}", flush=True)
        return MannequinFineTuneResponse(
            base64=img.b64_json,
            storage_uri=storage_uri,
            url=url,
            model=backend_model,
            revised_prompt=getattr(img, "revised_prompt", None),
        )
    except Exception as e:
        print(f"[mannequin/fine-tune] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"微调失败: {e}")


# ───────────────────────────────────────────────────────────────────────────
# 4. VLM 自动打标（读图 → 15 维度标签建议）
# ───────────────────────────────────────────────────────────────────────────

@router.post(
    "/auto-tag",
    response_model=MannequinAutoTagResponse,
    summary="4. VLM 读图自动打标签（gemini-3.7-flash，入库前必须步骤）",
    tags=["模特创建流程 · 交互端点"],
)
async def auto_tag_mannequin(body: MannequinAutoTagRequest):
    """VLM 读图，按 15 维度枚举自动打标 + 生成一句话描述。

    **流程定位**：**入库前必须步骤**，路径 A 和路径 B 都要走这个端点。

    **模型**：`google/gemini-3.7-flash`（多模态 VLM，chat_with_images）。

    **原理**：把最终入库的图（路径 A 原图 or 路径 B 微调/选中图）+ `MANNEQUIN_DIMENSION_GROUPS`
    枚举一起传给 VLM，让它按枚举值打标。返回的标签是**建议值**，前端必须展示给用户确认/修改后再入库。

    **安全**：返回的标签已经过后端校验——只保留在枚举里真实存在的值，不会有 VLM 幻觉。
    """
    from wellflow.app.llm.factory import get_llm_client

    # 目标图 → data URI
    data_uris = _uris_to_data_uris([body.image_uri])
    if not data_uris:
        raise HTTPException(400, f"无法读取 image_uri: {body.image_uri}")

    # 把维度枚举序列化成 JSON 给 VLM 参考
    dims_json = json_mod.dumps(MANNEQUIN_DIMENSION_GROUPS, ensure_ascii=False, indent=2)

    system = (
        "你是一个电商模特属性标注专家。根据提供的模特图片，"
        "从给定的维度枚举中选择最合适的值，以 JSON 格式返回。\n\n"
        "规则：\n"
        "1. 每个维度可多选（多值数组），也可以单选。\n"
        "2. 如果某维度无法从图中判断，返回空数组 []。\n"
        "3. 只使用枚举中出现的值，不要自己创造新值。\n"
        "4. 输出必须是一个合法的 JSON 对象，不要带 markdown 代码块标记或其他文字。"
    )

    user_text = (
        f"以下是维度枚举（JSON）：\n{dims_json}\n\n"
        f"{'用户补充意图：' + body.extra_context if body.extra_context else ''}\n\n"
        "请为这张模特图打标，严格按以下 JSON 格式返回：\n"
        "{\n"
        '  "tags": [\n'
        '    { "group_key": "...", "dim_key": "...", "tag_values": ["..."] }\n'
        "  ],\n"
        '  "description": "一句话描述这个模特，包含核心的身份/外貌/气质/风格信息",\n'
        '  "suggested_name": "可选的模特名字（2-4字中文；如果无法合适建议可以为 null）"\n'
        "}"
    )

    model_name = settings.llm_model_vlm
    print(f"[mannequin/auto-tag] 📤 调用 {model_name} 读图打标", flush=True)

    try:
        client = get_llm_client("vlm", model_override=model_name)
        resp = await client.chat_with_images(
            system=system,
            user=user_text,
            image_uris=data_uris,
            response_format={"type": "json_object"},
        )

        content = (resp.content or "").strip()
        # 容错：去掉 markdown 代码块
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(l for l in lines if not l.startswith("```"))

        data = json_mod.loads(content)
        raw_tags = data.get("tags", [])

        # 校验 + 过滤：只保留合法的 group_key / dim_key / tag_value
        valid_groups = MANNEQUIN_DIMENSION_GROUPS
        validated_tags: list[MannequinTagIn] = []
        for t in raw_tags:
            gk = t.get("group_key", "")
            dk = t.get("dim_key", "")
            vals = t.get("tag_values", [])
            if gk not in valid_groups or dk not in valid_groups[gk]:
                continue
            allowed = set(valid_groups[gk][dk])
            cleaned = [str(v) for v in vals if str(v) in allowed]
            if cleaned:
                validated_tags.append(MannequinTagIn(
                    group_key=gk, dim_key=dk, tag_values=cleaned,
                ))

        description = str(data.get("description", "")).strip()
        suggested_name = data.get("suggested_name")

        print(f"[mannequin/auto-tag] ✅ 标签 {len(validated_tags)} 组, "
              f"描述 {len(description)} chars", flush=True)

        return MannequinAutoTagResponse(
            tags=validated_tags,
            description=description,
            suggested_name=suggested_name if isinstance(suggested_name, str) else None,
            model=model_name,
        )

    except json_mod.JSONDecodeError as e:
        print(f"[mannequin/auto-tag] ❌ JSON 解析失败: {e}", flush=True)
        raise HTTPException(502, f"VLM 返回格式错误: {e}")
    except Exception as e:
        print(f"[mannequin/auto-tag] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"自动打标失败: {e}")
