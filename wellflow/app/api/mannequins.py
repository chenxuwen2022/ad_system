"""模特库 API（参考素材 > 模特库）。"""

from __future__ import annotations

import asyncio
import json as json_mod
from typing import Any

from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form
from sqlalchemy.orm import Session

from wellflow.app.config import settings
from wellflow.app.database import get_db
from wellflow.app.api.utils import ok, StandardResponse
from wellflow.app.repositories.mannequin_repo import MannequinRepo, MANNEQUIN_DIMENSION_GROUPS
from wellflow.app.schemas.asset_schemas import (
    MannequinUpdateRequest,
    MannequinListItem, MannequinListResponse,
    MannequinDetailResponse, MannequinDimensionsResponse,
    MannequinTagIn,
    MannequinOptimizePromptResponse,
    GeneratedImage, MannequinGenerateResponse,
    MannequinFineTuneResponse,
    MannequinAutoTagResponse,
)


router = APIRouter(prefix="/reference/mannequins")


def _storage_uri_url(uri: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith("http"):
        return uri
    return "/" + uri.lstrip("/")


def _tags_to_grouped(tags_rows) -> list[MannequinTagIn]:
    """把 repo.list_tags() 返回的已聚合 tag 列表转成 Pydantic 对象。"""
    return [
        MannequinTagIn(group_key=t["group_key"], dim_key=t["dim_key"], tag_values=t["tag_values"])
        for t in tags_rows
    ]


# ============================================================================
# 维度枚举（前端下拉菜单）
# ============================================================================

@router.get("/dimensions", response_model=StandardResponse[MannequinDimensionsResponse], summary="获取全部维度选项（前端筛选下拉菜单用）", tags=["模特库"])
def get_dimensions():
    """返回硬编码的维度分组和可选值。"""
    return ok(MannequinDimensionsResponse(groups=MANNEQUIN_DIMENSION_GROUPS))


# ============================================================================
# CRUD
# ============================================================================

@router.get("", response_model=StandardResponse[MannequinListResponse], summary="列出模特（支持 scope / q 搜索 / 多维筛选）", tags=["模特库"])
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

    return ok(MannequinListResponse(items=out_items, total=total, page=page, page_size=page_size))


@router.get("/{mannequin_id}", response_model=StandardResponse[MannequinDetailResponse], summary="查询模特详情", tags=["模特库"])
def get_mannequin(mannequin_id: int, db: Session = Depends(get_db)):
    repo = MannequinRepo(db)
    m = repo.get(mannequin_id)
    if not m:
        raise HTTPException(404, "模特不存在")
    tag_rows = repo.list_tags(mannequin_id)
    return ok(MannequinDetailResponse(
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
    ))


@router.post(
    "",
    response_model=StandardResponse[MannequinDetailResponse],
    summary="确认入库（唯一落盘点：cover_image + input_refs 二进制落盘 → 事务写 Mannequin + Tag + GenerateLog）",
    tags=["模特创建流程 · 入库端点"],
)
async def create_mannequin(
    # ── 必填字段 ──
    name: str = Form(...),
    cover_image: UploadFile = File(...),
    # ── 可选字段 ──
    en_name: str | None = Form(None),
    scope: str = Form("mine"),
    origin: str = Form("upload"),          # "upload" | "ai_generate"
    description: str | None = Form(None),
    tags: str | None = Form(None),          # JSON 字符串: [{group_key, dim_key, tag_values}, ...]
    # AI 生成上下文（路径 B 时传入）
    input_desc: str | None = Form(None),
    final_prompt: str | None = Form(None),
    generate_model: str | None = Form(None),
    num_output: int = Form(1),
    fine_tune_from: str | None = Form(None),
    fine_tune_prompt: str | None = Form(None),
    # input_refs（路径 B 的参考图，二进制数组）
    input_refs: list[UploadFile] = File(default_factory=list),
    db: Session = Depends(get_db),
):
    """模特最终入库 —— **整个流程唯一做文件落盘 + 写 DB 的端点**。

    **流程定位**：Stage 2 点「确认入库」才调用。

    **落盘策略**：先用 `uploads/mannequins/{uuid}/` 目录存 cover_image（封面图）
    和 input_refs（参考图），拿到 storage_uri 后传给 repo.create()。
    事务 commit 失败则 shutil.rmtree 清理已落盘目录。

    **落库**：一次事务写三张表：
    - `mannequin`：主表（name, cover_storage_uri, description, origin 等）
    - `mannequin_tag`：N 条标签（group_key / dim_key / tag_values）
    - `mannequin_generate_log`：路径 B 首轮 + 微调各一条（路径 A 跳过）
    """
    import uuid as _uuid
    from wellflow.app.utils.image_store import save_upload

    # ── 1. 解析 tags JSON ──
    parsed_tags: list[dict] | None = None
    if tags:
        try:
            parsed_tags = json_mod.loads(tags)
        except Exception:
            raise HTTPException(400, "tags 字段必须是合法 JSON")

    # ── 2. 先把所有文件读入内存（后续 try 里统一落盘，失败则清理） ──
    # save_upload 元组签名: (original_filename: str, raw_bytes: bytes, content_type: str | None)
    raw_cover = (
        cover_image.filename or "cover",
        await cover_image.read(),
        cover_image.content_type,
    )
    raw_refs = [
        (f.filename or "ref", await f.read(), f.content_type)
        for f in input_refs
    ]

    # ── 3. 落盘 ──
    dir_id = f"mq_{_uuid.uuid4().hex[:8]}"  # mq = mannequin pending
    storage_dir = f"uploads/mannequins/{dir_id}"
    try:
        # 封面图: uploads/mannequins/{uuid}/cover0.{ext}
        cover_paths = save_upload(f"mannequins/{dir_id}", [raw_cover], prefix="cover")
        cover_storage_uri = cover_paths[0] if cover_paths else None

        # 参考图
        ref_storage_uris: list[str] = []
        if raw_refs:
            ref_paths = save_upload(f"mannequins/{dir_id}", raw_refs, prefix="ref")
            ref_storage_uris = ref_paths

        if not cover_storage_uri:
            raise HTTPException(400, "封面图落盘失败")

        print(f"[mannequin/create] 💾 落盘目录 {storage_dir}, "
              f"cover={cover_storage_uri}, refs={len(ref_storage_uris)}", flush=True)

    except HTTPException:
        raise
    except Exception as e:
        # 落盘失败，清理
        _cleanup_storage_dir(storage_dir)
        raise HTTPException(500, f"图片落盘失败: {e}")

    # ── 4. 写 DB ──
    repo = MannequinRepo(db)
    try:
        m = repo.create(
            name=name,
            en_name=en_name,
            scope=scope,
            origin=origin,
            cover_storage_uri=cover_storage_uri,
            description=description,
            tags=parsed_tags,
            input_desc=input_desc,
            input_refs=ref_storage_uris or None,
            final_prompt=final_prompt,
            generate_model=generate_model,
            num_output=num_output,
            fine_tune_from=fine_tune_from,
            fine_tune_prompt=fine_tune_prompt,
        )
        db.commit()
        print(f"[mannequin/create] ✅ 入库成功 {m.mannequin_no} (id={m.id})", flush=True)
    except Exception as e:
        db.rollback()
        # DB 失败，清理已落盘的文件
        _cleanup_storage_dir(storage_dir)
        print(f"[mannequin/create] ❌ DB 写入失败，已清理 {storage_dir}: {e}", flush=True)
        raise HTTPException(400, f"创建失败: {e}")

    tag_rows = repo.list_tags(m.id)
    return ok(MannequinDetailResponse(
        id=m.id, mannequin_no=m.mannequin_no, name=m.name, en_name=m.en_name,
        scope=m.scope, origin=m.origin, status=m.status,
        cover_storage_uri=m.cover_storage_uri,
        cover_url=_storage_uri_url(m.cover_storage_uri),
        description=m.description,
        generate_model=m.generate_model, generate_prompt=m.generate_prompt,
        tags=_tags_to_grouped(tag_rows),
        created_at=m.created_at.isoformat(), updated_at=m.updated_at.isoformat(),
    ))


@router.put("/{mannequin_id}", response_model=StandardResponse[MannequinDetailResponse], summary="更新模特", tags=["模特库"])
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
    return ok(MannequinDetailResponse(
        id=m.id, mannequin_no=m.mannequin_no, name=m.name, en_name=m.en_name,
        scope=m.scope, origin=m.origin, status=m.status,
        cover_storage_uri=m.cover_storage_uri,
        cover_url=_storage_uri_url(m.cover_storage_uri),
        description=m.description,
        generate_model=m.generate_model, generate_prompt=m.generate_prompt,
        tags=_tags_to_grouped(tag_rows),
        created_at=m.created_at.isoformat(), updated_at=m.updated_at.isoformat(),
    ))


@router.delete("/{mannequin_id}", response_model=StandardResponse[dict], summary="删除模特（级联清理标签，生成日志保留）", tags=["模特库"])
def delete_mannequin(mannequin_id: int, db: Session = Depends(get_db)):
    repo = MannequinRepo(db)
    ok_repo = repo.delete(mannequin_id)
    if not ok_repo:
        raise HTTPException(404, "模特不存在")
    db.commit()
    return ok({"deleted": True, "mannequin_id": mannequin_id})


# ============================================================================
# 模特创建流程 —— 交互端点（全 multipart，无落盘，只读二进制喂 LLM）
# ============================================================================

def _cleanup_storage_dir(storage_uri: str) -> None:
    """安全清理 storage_uri 对应的上传目录（入库失败时回滚文件落盘用）。"""
    import shutil as _shutil
    from pathlib import Path
    from wellflow.app.utils.image_store import _get_upload_dir
    abs_path = (_get_upload_dir().parent / storage_uri).resolve()
    upload_dir = _get_upload_dir().resolve()
    # 安全校验：目录必须在 upload_dir 之下
    if upload_dir in abs_path.parents and abs_path.exists() and abs_path.is_dir():
        _shutil.rmtree(abs_path, ignore_errors=True)
        print(f"[mannequin] 🗑️ 清理落盘目录 {abs_path}", flush=True)


async def _files_to_data_uris(files: list[UploadFile]) -> list[str]:
    """把 multipart UploadFile 列表 → data URI 列表（不落盘）。"""
    from wellflow.app.utils.image_store import bytes_items_to_data_uris
    items: list[tuple[bytes, str]] = []
    for f in files:
        raw = await f.read()
        mime = f.content_type or "image/jpeg"
        items.append((raw, mime))
    return bytes_items_to_data_uris(items)


# ───────────────────────────────────────────────────────────────────────────
# 废弃的上传端点 —— 不再需要了
# ───────────────────────────────────────────────────────────────────────────

# 原 POST /reference/mannequins/upload 已废弃。
# 整个模特创建流程不再调用任何图片上传端点（包括通用 /api/wellflow/image/uploads）。
# 所有图片都以二进制流形式在前端和交互端点之间「过路」，只在最终 POST /reference/mannequins
# 入库端点里统一落盘。


# ───────────────────────────────────────────────────────────────────────────
# 1. 优化提示词（multipart，可选，LLM 读参考图 + 文本 → 生成英文 prompt）
# ───────────────────────────────────────────────────────────────────────────

PROMPT_REF_FIX_SUFFIX = "\n\nStrictly follow the facial features of the current model reference images."  # 生成模型是英文的，所以 suffix 也用英文；用户要求中文"严格参照当前模特参考图的五官"的语义已包含在其中


@router.post(
    "/optimize-prompt",
    response_model=StandardResponse[MannequinOptimizePromptResponse],
    summary="1. 优化提示词（multipart，读 ref_images 二进制 + 文本 → 带固定话术的英文 prompt）",
    tags=["模特创建流程 · 交互端点"],
)
async def optimize_prompt(
    raw_prompt: str = Form(...),
    tags: str | None = Form(None),                    # JSON 字符串: [{group_key, dim_key, tag_values}, ...]
    ref_images: list[UploadFile] = File(default_factory=list),
):
    """把用户原始描述 + 维度标签 + 参考图 → 结构化英文生图 prompt。

    **流程定位**：路径 B（AI 生成）的 **可选步骤**。前端点「✨ 优化提示词」才调用。
    不点可以跳过，直接用原始 prompt 调 `/generate`。

    **入参**：全部 multipart。参考图以二进制形式传入，后端直接转 data URI 喂 LLM（不落盘）。

    **固定话术**：返回的 final_prompt 末尾会自动拼接
    `"Strictly follow the facial features of the current model reference images."`
    确保生成图五官严格参照参考图。
    """
    from wellflow.app.llm.factory import get_llm_client

    # 解析 tags JSON
    parsed_tags: list[dict] = []
    if tags:
        try:
            parsed_tags = json_mod.loads(tags)
        except Exception:
            raise HTTPException(400, "tags 必须是合法 JSON")

    # 参考图 → data URI（不落盘）
    ref_data_uris = await _files_to_data_uris(ref_images) if ref_images else []

    # 把标签转成可读的中文描述
    tag_lines: list[str] = []
    for t in parsed_tags:
        gk, dk = t.get("group_key", ""), t.get("dim_key", "")
        vals = " / ".join(t.get("tag_values", []))
        if gk and dk and vals:
            tag_lines.append(f"  - {gk} > {dk}: {vals}")
    tags_text = "\n".join(tag_lines) if tag_lines else "（未选择任何维度标签）"

    system = (
        "You are a professional e-commerce model image prompt optimization expert. "
        "Your task is to integrate the user's raw description and dimension tags into a structured, "
        "detailed English prompt suitable for AI image generation models like GPT Image.\n\n"
        "Rules:\n"
        "1. Preserve the user's core intent. Do not invent new attributes.\n"
        "2. Naturally incorporate dimension tags into the description.\n"
        "3. Output ONLY the English prompt. No explanations, no prefixes, no suffixes.\n"
        "4. Describe clothing display naturally if the user mentions specific garments."
    )

    user_text = (
        f"Raw description: {raw_prompt}\n\n"
        f"Dimension tags:\n{tags_text}\n\n"
        f"Reference images provided: {len(ref_data_uris)} image(s)\n\n"
        f"Please output the optimized English prompt:"
    )

    model_name = settings.llm_model_text
    print(f"[mannequin/optimize-prompt] 📤 {model_name}, refs={len(ref_data_uris)}", flush=True)

    try:
        client = get_llm_client("vlm", model_override=model_name)

        # 有参考图 → 用 chat_with_images（让 LLM 看图理解用户要的模特五官）
        if ref_data_uris:
            resp = await client.chat_with_images(
                system=system,
                user=user_text,
                image_uris=ref_data_uris,
            )
        else:
            resp = await client.chat(
                system=system,
                user=user_text,
                temperature=0.3,
            )

        final_prompt = (resp.content or "").strip()
        if not final_prompt:
            raise RuntimeError("LLM 返回空内容")

        # 末尾拼固定话术
        final_prompt = final_prompt + PROMPT_REF_FIX_SUFFIX

        print(f"[mannequin/optimize-prompt] ✅ {len(final_prompt)} chars", flush=True)
        return ok(MannequinOptimizePromptResponse(final_prompt=final_prompt))

    except Exception as e:
        print(f"[mannequin/optimize-prompt] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"提示词优化失败: {e}")


# ───────────────────────────────────────────────────────────────────────────
# 2. 首轮批量生图（multipart，不落盘，只返回 base64）
# ───────────────────────────────────────────────────────────────────────────

@router.post(
    "/generate",
    response_model=StandardResponse[MannequinGenerateResponse],
    summary="2. 首轮批量生图（multipart，不落盘，只返回 base64）",
    tags=["模特创建流程 · 交互端点"],
)
async def generate_mannequin_images(
    prompt: str = Form(...),
    generate_model: str = Form("gpt-image-2"),
    num_output: int = Form(3),
    size: str = Form("1024x1536"),
    ref_images: list[UploadFile] = File(default_factory=list),
):
    """调用生图模型批量生成 n 张模特图 —— **不落盘**，只返回 base64。

    **流程定位**：路径 B（AI 生成）的 **必须步骤**。

    **入参**：全部 multipart。参考图二进制 → data URI 直接喂生图模型。

    **返回**：images[] 每项只有 index + base64。**没有 storage_uri / url / revised_prompt**。
    生成图在整个准备阶段都只活在前端内存里。
    """
    import asyncio as _asyncio
    from wellflow.app.llm.factory import get_llm_client

    backend_model = generate_model.strip()
    num_output = max(1, min(num_output, 6))
    client = get_llm_client("image", model_override=backend_model)

    ref_data_uris = await _files_to_data_uris(ref_images) if ref_images else None

    sem = _asyncio.Semaphore(settings.node3_gen_concurrency)

    print(f"[mannequin/generate] 📤 model={backend_model} n={num_output} "
          f"refs={len(ref_data_uris) if ref_data_uris else 0} size={size}", flush=True)

    async def _one(i: int):
        async with sem:
            r = await client.generate_image(
                prompt=prompt,
                image_uris=ref_data_uris,
                size=size,
                n=1,
                response_format="b64_json",
            )
            img = r.all_images[0]
            return GeneratedImage(index=i, base64=img.b64_json)

    try:
        tasks = [_one(i + 1) for i in range(num_output)]
        results = await _asyncio.gather(*tasks, return_exceptions=True)

        images: list[GeneratedImage] = []
        for r in results:
            if isinstance(r, Exception):
                print(f"[mannequin/generate] ⚠️ 一张失败: {r}", flush=True)
                continue
            images.append(r)

        if not images:
            raise HTTPException(502, "全部生图失败")

        print(f"[mannequin/generate] ✅ {len(images)}/{num_output}", flush=True)
        return ok(MannequinGenerateResponse(
            images=images,
            model=backend_model,
            final_prompt=prompt,
        ))

    except HTTPException:
        raise
    except Exception as e:
        print(f"[mannequin/generate] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"生图失败: {e}")


# ───────────────────────────────────────────────────────────────────────────
# 3. 单张微调（multipart，不落盘，图生图）
# ───────────────────────────────────────────────────────────────────────────

@router.post(
    "/fine-tune",
    response_model=StandardResponse[MannequinFineTuneResponse],
    summary="3. 单张微调（multipart，不落盘，图生图）",
    tags=["模特创建流程 · 交互端点"],
)
async def fine_tune_mannequin(
    target_image: UploadFile = File(...),
    tune_prompt: str = Form(...),
    original_prompt: str | None = Form(None),
    generate_model: str = Form("gpt-image-2"),
    size: str = Form("1024x1536"),
    ref_images: list[UploadFile] = File(default_factory=list),
):
    """对选中的一张模特图做图生图微调 —— **不落盘**，只返回 base64。

    **流程定位**：路径 B（AI 生成）的 **可选步骤**。

    **入参**：全部 multipart。target_image 是前端选中的那张生成图（base64 → 二进制），
    ref_images 是原始参考图（可选保留）。

    **微调 prompt 组合**：保持身份一致性 + 用户描述的修改内容。
    """
    from wellflow.app.llm.factory import get_llm_client

    backend_model = generate_model.strip()
    client = get_llm_client("image", model_override=backend_model)

    # target_image + ref_images → data URI（不落盘）
    target_data_uris = await _files_to_data_uris([target_image])
    ref_data_uris = await _files_to_data_uris(ref_images) if ref_images else []

    if not target_data_uris:
        raise HTTPException(400, "target_image 读取失败")

    # 微调 prompt = 身份维持 + 修改内容
    identity = original_prompt or "Maintain the model's facial features, hairstyle, overall temperament and identity."
    tune_prompt_final = f"{identity}, modifications: {tune_prompt}"

    all_data_uris = target_data_uris + ref_data_uris

    print(f"[mannequin/fine-tune] 📤 model={backend_model} images={len(all_data_uris)}", flush=True)

    try:
        r = await client.generate_image(
            prompt=tune_prompt_final,
            image_uris=all_data_uris,
            size=size,
            n=1,
            response_format="b64_json",
        )
        img = r.all_images[0]
        print(f"[mannequin/fine-tune] ✅", flush=True)
        return ok(MannequinFineTuneResponse(
            base64=img.b64_json,
            model=backend_model,
        ))
    except Exception as e:
        print(f"[mannequin/fine-tune] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"微调失败: {e}")


# ───────────────────────────────────────────────────────────────────────────
# 4. VLM 自动打标（multipart，读图 → 15 维度标签建议）
# ───────────────────────────────────────────────────────────────────────────

@router.post(
    "/auto-tag",
    response_model=StandardResponse[MannequinAutoTagResponse],
    summary="4. VLM 读图自动打标签（multipart，不落盘，入库前必须步骤）",
    tags=["模特创建流程 · 交互端点"],
)
async def auto_tag_mannequin(
    files: list[UploadFile] = File(...),
    extra_context: str | None = Form(None),
):
    """VLM 读图，按 15 维度枚举自动打标 + 生成一句话描述。

    **流程定位**：**入库前必须步骤**，路径 A 和路径 B 都要走这个端点。

    **入参**：全部 multipart。files[0] 作为目标图（路径 A 原图 or 路径 B 选中图），
    二进制 → data URI → VLM（不落盘）。

    **安全**：返回的标签已经过后端白名单校验 —— 只保留 MANNEQUIN_DIMENSION_GROUPS 枚举内真实存在的值。
    """
    from wellflow.app.llm.factory import get_llm_client

    if not files:
        raise HTTPException(400, "至少要传 1 张图片")

    # 目标图 → data URI（不落盘）
    data_uris = await _files_to_data_uris(files[:1])

    # 把维度枚举序列化成 JSON
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
        f"{'用户补充意图：' + extra_context if extra_context else ''}\n\n"
        "请为这张模特图打标，严格按以下 JSON 格式返回：\n"
        "{\n"
        '  "tags": [\n'
        '    { "group_key": "...", "dim_key": "...", "tag_values": ["..."] }\n'
        "  ],\n"
        '  "description": "一句话描述这个模特，包含核心的身份/外貌/气质/风格信息",\n'
        '  "suggested_name": "可选的模特名字（2-4字中文；如果无法合适建议可以为 null）"\n'
        "}"
    )

    model_name = settings.llm_model_node3
    print(f"[mannequin/auto-tag] 📤 {model_name}", flush=True)

    try:
        client = get_llm_client("vlm", model_override=model_name)
        resp = await client.chat_with_images(
            system=system,
            user=user_text,
            image_uris=data_uris,
            response_format={"type": "json_object"},
        )

        content = (resp.content or "").strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(l for l in lines if not l.startswith("```"))

        data = json_mod.loads(content)
        raw_tags = data.get("tags", [])

        # 白名单校验
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

        print(f"[mannequin/auto-tag] ✅ tags={len(validated_tags)} desc={len(description)}", flush=True)

        return ok(MannequinAutoTagResponse(
            tags=validated_tags,
            description=description,
            suggested_name=suggested_name if isinstance(suggested_name, str) else None,
            model=model_name,
        ))

    except json_mod.JSONDecodeError as e:
        print(f"[mannequin/auto-tag] ❌ JSON 解析失败: {e}", flush=True)
        raise HTTPException(502, f"VLM 返回格式错误: {e}")
    except Exception as e:
        print(f"[mannequin/auto-tag] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"自动打标失败: {e}")
