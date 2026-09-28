"""模特库 API（参考素材 > 模特库）。"""

from __future__ import annotations

import asyncio
import json as json_mod
from typing import Any

from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form
from sqlalchemy.orm import Session

from wellflow.app.llm.base import InsufficientCreditsError
from wellflow.app.config import settings
from wellflow.app.database import get_db
from wellflow.app.api.utils import ok, StandardResponse, to_cn_iso
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


@router.get(
    "/ai-status",
    response_model=StandardResponse[dict],
    summary="轮询异步入库任务状态(必须注册在动态路由之前,防 /{mannequin_id} 抢匹配)",
    tags=["模特创建流程 · 异步入口"],
)
async def mannequin_ai_status(task_id: str):
    from fastapi.concurrency import run_in_threadpool
    store = _mq_task_store()
    await run_in_threadpool(store.sweep)
    t = await asyncio.to_thread(store.load, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return ok(t)


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
            created_at=to_cn_iso(m.created_at),
            updated_at=to_cn_iso(m.updated_at),
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
        created_at=to_cn_iso(m.created_at),
        updated_at=to_cn_iso(m.updated_at),
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
        created_at=to_cn_iso(m.created_at), updated_at=to_cn_iso(m.updated_at),
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
        created_at=to_cn_iso(m.created_at), updated_at=to_cn_iso(m.updated_at),
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
        "你是一名专业的电商模特图提示词优化专家，负责把用户的原始描述和维度标签整合成一条"
        "结构化、细节丰富的中文提示词，用于 GPT Image 等 AI 图像生成模型。\n\n"
        "规则：\n"
        "1. 保留用户核心意图，不要凭空创造属性。\n"
        "2. 把维度标签自然融入描述。\n"
        "3. 只输出最终的中文提示词本身，不要解释、不要前后缀、不要引号。\n"
        "4. 如果用户提到了具体服装，要自然描述服装的穿着与展示效果。\n"
        "5. 优先使用中文表达；如果某些风格、材质或摄影术语用英文更自然（如 soft lighting、"
        "cinematic、Denim 等），可以保留，但整体提示词应以中文为主。"
    )

    user_text = (
        f"用户原始描述：{raw_prompt}\n\n"
        f"维度标签：\n{tags_text}\n\n"
        f"参考图数量：{len(ref_data_uris)} 张\n\n"
        f"请输出优化后的中文提示词："
    )

    from wellflow.app.llm.model_pool import get_model_pool

    print(f"[mannequin/optimize-prompt] 📤 model_pool, refs={len(ref_data_uris)}", flush=True)

    try:
        pool = get_model_pool()

        # 有参考图 → 走多模态接口让 VLM 看图理解用户要的模特五官
        if ref_data_uris:
            resp, used_model = await pool.chat_with_images(
                system=system,
                user=user_text,
                image_uris=ref_data_uris,
                reasoning_effort=settings.text_reasoning_effort,
            )
        else:
            resp, used_model = await pool.chat(
                system=system,
                user=user_text,
                temperature=0.3,
                reasoning_effort=settings.text_reasoning_effort,
            )

        final_prompt = (resp.content or "").strip()
        if not final_prompt:
            raise RuntimeError("LLM 返回空内容")

        # 末尾拼固定话术
        final_prompt = final_prompt + PROMPT_REF_FIX_SUFFIX

        print(f"[mannequin/optimize-prompt] ✅ {len(final_prompt)} chars", flush=True)
        return ok(MannequinOptimizePromptResponse(final_prompt=final_prompt))

    except InsufficientCreditsError as e:
        print(f"[mannequin/optimize-prompt] ❌ 上游额度不足: {e.upstream_message}", flush=True)
        raise HTTPException(402, f"上游账户额度不足，无法优化提示词。请联系管理员充值：{e.upstream_message}")
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
    generate_model: str = Form("qwen-image-3.0"),
    num_output: int = Form(3),
    size: str = Form("1024x1536"),
    ref_images: list[UploadFile] = File(default_factory=list),
):
    """调用生图模型批量生成 n 张模特图 —— **不落盘**，只返回 base64。

    **流程定位**：路径 B（AI 生成）的 **必须步骤**。

    **入参**：全部 multipart。参考图二进制 → data URI 直接喂生图模型。
    **generate_model** 指定本轮生图模型，失败不会自动切换模型。

    **返回**：images[] 每项只有 index + base64。**没有 storage_uri / url / revised_prompt**。
    生成图在整个准备阶段都只活在前端内存里。
    """
    import asyncio as _asyncio
    from wellflow.app.llm.image_gen_service import generate_single_image

    num_output = max(1, min(num_output, 6))

    ref_data_uris = await _files_to_data_uris(ref_images) if ref_images else None

    sem = _asyncio.Semaphore(settings.mannequins_gen_concurrency)

    print(f"[mannequin/generate] 📤 n={num_output} "
          f"refs={len(ref_data_uris) if ref_data_uris else 0} size={size} "
          f"model={generate_model}", flush=True)

    # 收集成功生图实际使用的模型名
    _used_models: list[str] = []

    async def _one(i: int) -> GeneratedImage:
        async with sem:
            r = await generate_single_image(
                model=generate_model,
                prompt=prompt,
                size=size,
                ref_data_uris=ref_data_uris,
            )
            if r.model:
                _used_models.append(r.model)
            img = r.all_images[0]
            return GeneratedImage(index=i, base64=img.b64_json)

    try:
        tasks = [_asyncio.create_task(_one(i + 1)) for i in range(num_output)]

        credits_failure: InsufficientCreditsError | None = None
        normal_failures: list[Exception] = []
        images: list[GeneratedImage] = []
        pending_set = set(tasks)

        # 用 FIRST_COMPLETED 循环 —— 一旦任何一个任务爆出 InsufficientCreditsError，
        # 立刻取消剩余未完成任务，避免继续打到上游浪费配额/重复刷 402 日志
        while pending_set:
            done, pending_set = await _asyncio.wait(pending_set, return_when=_asyncio.FIRST_COMPLETED)
            for fut in done:
                try:
                    res = await fut
                except InsufficientCreditsError as e:
                    credits_failure = e
                    break  # 跳出内层，外层 while 会因为 pending_set 处理后续
                except Exception as e:
                    normal_failures.append(e)
                    continue
                images.append(res)
            if credits_failure is not None:
                break

        # 统一取消剩余
        for fut in pending_set:
            fut.cancel()

        if credits_failure is not None:
            print(f"[mannequin/generate] ❌ 上游额度不足，全部取消: {credits_failure.upstream_message}", flush=True)
            raise HTTPException(
                status_code=402,
                detail=f"上游账户额度不足，无法生成模特图。请联系管理员充值：{credits_failure.upstream_message}",
            )

        if not images:
            raise HTTPException(502, f"全部生图失败: {normal_failures[0] if normal_failures else '未知原因'}")

        # 按 index 稳定排序，让 UI 展示顺序跟用户预期一致
        images.sort(key=lambda g: g.index)

        # 返回实际用到的模型名（取第一个去重后的，通常全成功时都是同一个）
        actual_model = _used_models[0] if _used_models else "(unknown)"
        print(f"[mannequin/generate] ✅ {len(images)}/{num_output} model={actual_model}", flush=True)
        return ok(MannequinGenerateResponse(
            images=images,
            model=actual_model,
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
    generate_model: str = Form("qwen-image-3.0"),
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
    except InsufficientCreditsError as e:
        print(f"[mannequin/fine-tune] ❌ 上游额度不足: {e.upstream_message}", flush=True)
        raise HTTPException(
            status_code=402,
            detail=f"上游账户额度不足，无法微调模特图。请联系管理员充值：{e.upstream_message}",
        )
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

    from wellflow.app.llm.model_pool import get_model_pool

    print(f"[mannequin/auto-tag] 📤 model_pool", flush=True)

    try:
        pool = get_model_pool()
        resp, used_model = await pool.chat_with_images(
            system=system,
            user=user_text,
            image_uris=data_uris,
            response_format={"type": "json_object"},
            reasoning_effort=settings.text_reasoning_effort,
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
            model=used_model,
        ))

    except InsufficientCreditsError as e:
        print(f"[mannequin/auto-tag] ❌ 上游额度不足: {e.upstream_message}", flush=True)
        raise HTTPException(
            status_code=402,
            detail=f"上游账户额度不足，无法自动打标。请联系管理员充值：{e.upstream_message}",
        )
    except json_mod.JSONDecodeError as e:
        print(f"[mannequin/auto-tag] ❌ JSON 解析失败: {e}", flush=True)
        raise HTTPException(502, f"VLM 返回格式错误: {e}")
    except Exception as e:
        print(f"[mannequin/auto-tag] ❌ 失败: {e}", flush=True)
        raise HTTPException(502, f"自动打标失败: {e}")


# ============================================================================
# 异步一键入库(2026-09-22 新增 —— 技术主管要求:上传已有模特 → 点击直接入库
# → 列表转圈「正在入库」→ 后台自动打标 → 转正式。老端点一律不动)
# ============================================================================

def _mq_data_uris_of_cover(uri: str) -> list[str]:
    """封面 storage_uri → data URI 列表(复用统一图片工具)。"""
    from wellflow.app.utils.image_store import paths_to_data_uris
    return paths_to_data_uris([uri])


def _mq_mark_failed_sync(mannequin_id: int) -> None:
    """sweep 行联动(同步,跑在线程池):行非 active 时标 failed。"""
    from wellflow.app.database import session_scope as _ss
    from wellflow.app.models.mannequin_models import Mannequin as _M
    try:
        with _ss() as db:
            m = db.get(_M, mannequin_id)
            if m is not None and m.status != "active":
                m.status = "failed"
                db.commit()
    except Exception:
        pass


def _mq_task_store():
    """惰性构造 TaskStore(避免 import 期建目录)。"""
    from pathlib import Path as _Path
    from wellflow.app.utils.async_task_lib import TaskStore
    return TaskStore(
        _Path(__file__).resolve().parents[3] / "task_state" / "mannequin_ai",
        row_id_field="mannequin_id",
        mark_row_failed=_mq_mark_failed_sync,
    )


_MQ_BG_TASKS: "set" = set()
_MQ_SEMAPHORE = asyncio.Semaphore(3)


async def _mq_gen_no(db) -> str:
    """异步版编号生成(独立实现,不碰 MannequinRepo)。"""
    import re as _re
    from sqlalchemy import select as _select
    from wellflow.app.models.mannequin_models import Mannequin as _M
    rows = (await db.execute(_select(_M.mannequin_no))).scalars().all()
    nums = []
    for val in rows:
        m = _re.search(r"(\d+)$", val or "")
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    return f"WF-M{n:03d}"


async def _mq_auto_tag(data_uris: list[str], extra_context: str | None = None):
    """独立打标实现(参考老 auto_tag 端点的 VLM+白名单思路,老端点零改动)。

    返回 (validated_tags[{group_key,dim_key,tag_values}], description, suggested_name)。
    """
    from wellflow.app.llm.model_pool import get_model_pool

    dims_json = json_mod.dumps(MANNEQUIN_DIMENSION_GROUPS, ensure_ascii=False, indent=2)
    system = (
        "你是一个电商模特属性标注专家。根据提供的模特图片,"
        "从给定的维度枚举中选择最合适的值,以 JSON 格式返回。\n\n"
        "规则:\n1. 每个维度可多选(多值数组),也可以单选。\n"
        "2. 如果某维度无法从图中判断,返回空数组 []。\n"
        "3. 只使用枚举中出现的值,不要自己创造新值。\n"
        "4. 输出必须是一个合法的 JSON 对象,不要带 markdown 代码块标记或其他文字。"
    )
    user_text = (
        f"以下是维度枚举(JSON):\n{dims_json}\n\n"
        f"{'用户补充意图:' + extra_context if extra_context else ''}\n\n"
        "请为这张模特图打标,严格按以下 JSON 格式返回:\n"
        "{\n  \"tags\": [\n    { \"group_key\": \"...\", \"dim_key\": \"...\", \"tag_values\": [\"...\"] }\n  ],\n"
        '  "description": "一句话描述这个模特,包含核心的身份/外貌/气质/风格信息",\n'
        '  "suggested_name": "可选的模特名字(2-4字中文;无法合适建议可以为 null)"\n}'
    )
    pool = get_model_pool()
    resp, _used_model = await pool.chat_with_images(
        system=system, user=user_text, image_uris=data_uris,
        response_format={"type": "json_object"}, reasoning_effort="close",
    )
    content = (resp.content or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(l for l in lines if not l.startswith("```"))
    data = json_mod.loads(content)

    validated_tags: list[dict] = []
    for t in data.get("tags", []):
        gk = t.get("group_key", "")
        dk = t.get("dim_key", "")
        vals = t.get("tag_values", [])
        if gk not in MANNEQUIN_DIMENSION_GROUPS or dk not in MANNEQUIN_DIMENSION_GROUPS[gk]:
            continue
        allowed = set(MANNEQUIN_DIMENSION_GROUPS[gk][dk])
        cleaned = [str(v) for v in vals if str(v) in allowed]
        if cleaned:
            validated_tags.append({"group_key": gk, "dim_key": dk, "tag_values": cleaned})
    description = str(data.get("description", "")).strip()
    suggested = data.get("suggested_name")
    return validated_tags, description, (suggested if isinstance(suggested, str) else None)


async def _run_mannequin_async(task: dict) -> None:
    """后台任务:自动打标 → 更新行(tags/description)→ active;失败 → failed(打标失败降级不阻断)。"""
    from wellflow.app.database import AsyncSessionLocal as _ASL
    from wellflow.app.models.mannequin_models import Mannequin as _M, MannequinTag as _MT

    store = _mq_task_store()
    from wellflow.app.utils.async_task_lib import spawn_heartbeat
    spawn_heartbeat(store, task["task_id"])
    mannequin_id = task.get("mannequin_id")

    async def fail(err: str) -> None:
        task["status"] = "failed"
        task["error"] = err
        await asyncio.to_thread(store.save, task)
        try:
            async with _ASL() as db:
                m = await db.get(_M, int(mannequin_id)) if mannequin_id else None
                if m is not None and m.status != "active":
                    m.status = "failed"
                    await db.commit()
        except Exception:
            pass

    async with _MQ_SEMAPHORE:
        try:
            task["step"] = "tagging"
            await asyncio.to_thread(store.save, task)
            try:
                validated_tags, description, _suggested = await _mq_auto_tag(
                    task["data_uris"], task.get("extra_context"))
            except Exception:
                # 打标失败降级:不阻断,行仍转正式(标签/描述留空,用户可手补)
                validated_tags, description = [], ""

            task["step"] = "saving"
            await asyncio.to_thread(store.save, task)
            try:
                async with _ASL() as db:
                    m = await db.get(_M, int(mannequin_id))
                    if m is None:
                        raise RuntimeError(f"模特 {mannequin_id} 已被删除")
                    if validated_tags:
                        for t in validated_tags:
                            for val in t["tag_values"]:
                                db.add(_MT(
                                    mannequin_id=m.id,
                                    group_key=t["group_key"],
                                    dim_key=t["dim_key"],
                                    tag_value=val,
                                ))
                    if description:
                        m.description = description
                    m.status = "active"
                    await db.commit()
            except Exception as e:
                await fail(f"入库资料写入失败: {str(e)[:400]}")
                return

            task["status"] = "done"
            task["error"] = ""
            await asyncio.to_thread(store.save, task)
        except Exception as e:
            await fail(f"处理异常: {str(e)[:400]}")


@router.post(
    "/async-generate",
    response_model=StandardResponse[dict],
    summary="一键异步入库(上传已有模特:点击即入库 → 列表转圈 → 后台打标 → 转正式)",
    tags=["模特创建流程 · 异步入口"],
)
async def async_generate_mannequin(
    name: str = Form(...),
    cover_image: UploadFile = File(...),
    en_name: str | None = Form(None),
    scope: str = Form("mine"),
    extra_context: str | None = Form(None),
):
    """新增异步入口(不动老 create_mannequin):上传图落盘 → 立即写行 generating → 后台打标。"""
    import os
    import uuid as _uuid
    import time as _time
    from sqlalchemy.exc import IntegrityError
    from fastapi.concurrency import run_in_threadpool
    from wellflow.app.database import AsyncSessionLocal as _ASL
    from wellflow.app.models.mannequin_models import Mannequin as _M
    from wellflow.app.utils.image_store import save_upload

    store = _mq_task_store()
    await run_in_threadpool(store.sweep)

    # ── 1. 上传图落盘(与老 create_mannequin 同一套 save_upload) ──
    raw_cover = (cover_image.filename or "cover", await cover_image.read(), cover_image.content_type)
    dir_id = f"mq_async_{_uuid.uuid4().hex[:8]}"
    try:
        cover_paths = save_upload(f"mannequins/{dir_id}", [raw_cover], prefix="cover")
        cover_uri = cover_paths[0] if cover_paths else None
    except Exception as e:
        raise HTTPException(500, f"图片落盘失败: {e}")
    if not cover_uri:
        raise HTTPException(400, "封面图落盘失败")

    # ── 2. 立即写行(status=generating;批量并发撞 mannequin_no 时重试一次) ──
    m = None
    last_err: Exception | None = None
    for _attempt in range(2):
        try:
            async with _ASL() as db:
                m = _M(
                    mannequin_no=await _mq_gen_no(db),
                    name=name.strip(),
                    en_name=en_name,
                    scope=scope,
                    origin="upload",
                    cover_storage_uri=cover_uri,
                    status="generating",
                )
                db.add(m)
                await db.commit()
            break
        except IntegrityError:
            last_err = IntegrityError("mannequin_no 冲突")
    if m is None:
        raise HTTPException(400, f"创建失败: {last_err}")

    # ── 3. 建任务 → 起后台协程 → 秒回 ──
    task_id = f"{int(_time.time() * 1000)}_{_uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "task_type": "mannequin", "pid": str(os.getpid()),
        "status": "processing", "step": "queued",
        "mannequin_id": m.id, "cover_uri": cover_uri,
        "data_uris": _mq_data_uris_of_cover(cover_uri),
        "extra_context": extra_context, "error": "",
    }
    await asyncio.to_thread(store.save, task)
    _bg = asyncio.create_task(_run_mannequin_async(task))
    _MQ_BG_TASKS.add(_bg)
    _bg.add_done_callback(_MQ_BG_TASKS.discard)
    return ok({
        "mannequin_id": m.id, "mannequin_no": m.mannequin_no,
        "task_id": task_id, "status": "generating",
    })


# ============================================================================
# v3:生成路径四步异步化(2026-09-22 追加;老端点一律不动)
# 优化提示词 / 生成模特 / 微调 / 识别入库资料 —— 「秒回 task_id → 后台跑 → 轮询」
# ============================================================================

# 模特生图降级链:前端传的模型放首位,失败自动换下一个(qwen-image-3.0 实测可用;
# gpt-image-2 渠道不稳且有人物安全拦截,故链里靠后)
_MQ_IMAGE_FALLBACKS = ["qwen-image-3.0", "gpt-image-2", "gpt-image-2.5-flare", "mai-image-2.5"]


def _mq_model_chain(preferred: str) -> list[str]:
    chain = [preferred] if preferred else []
    for m in _MQ_IMAGE_FALLBACKS:
        if m not in chain:
            chain.append(m)
    return chain


def _mq_save_refs(raw_refs: list) -> list[str]:
    """参考图落盘 → storage_uri 列表(任务文件只存 uri,不存 base64)。"""
    import uuid as _uuid
    from wellflow.app.utils.image_store import save_upload
    if not raw_refs:
        return []
    dir_id = f"mq_refs_{_uuid.uuid4().hex[:8]}"
    return save_upload(f"mannequins/{dir_id}", raw_refs, prefix="ref")


async def _mq_async_optimize(raw_prompt: str, tags: str | None, ref_uris: list[str]) -> str:
    """独立实现(参考老 optimize_prompt 端点逻辑,老端点零改动)。"""
    from wellflow.app.llm.model_pool import get_model_pool
    from wellflow.app.utils.image_store import paths_to_data_uris

    parsed_tags: list[dict] = []
    if tags:
        try:
            parsed_tags = json_mod.loads(tags)
        except Exception:
            parsed_tags = []

    tag_lines: list[str] = []
    for t in parsed_tags:
        gk, dk = t.get("group_key", ""), t.get("dim_key", "")
        vals = " / ".join(t.get("tag_values", []))
        if gk and dk and vals:
            tag_lines.append(f"  - {gk} > {dk}: {vals}")
    tags_text = "\n".join(tag_lines) if tag_lines else "（未选择任何维度标签）"

    ref_data_uris = paths_to_data_uris(ref_uris) if ref_uris else []

    system = (
        "你是一名专业的电商模特图提示词优化专家，负责把用户的原始描述和维度标签整合成一条"
        "结构化、细节丰富的中文提示词，用于 GPT Image 等 AI 图像生成模型。\n\n"
        "规则：\n"
        "1. 保留用户核心意图，不要凭空创造属性。\n"
        "2. 把维度标签自然融入描述。\n"
        "3. 只输出最终的中文提示词本身，不要解释、不要前后缀、不要引号。\n"
        "4. 如果用户提到了具体服装，要自然描述服装的穿着与展示效果。\n"
        "5. 优先使用中文表达；如果某些风格、材质或摄影术语用英文更自然（如 soft lighting、"
        "cinematic、Denim 等），可以保留，但整体提示词应以中文为主。"
    )
    user_text = (
        f"用户原始描述：{raw_prompt}\n\n"
        f"维度标签：\n{tags_text}\n\n"
        f"参考图数量：{len(ref_data_uris)} 张\n\n"
        f"请输出优化后的中文提示词："
    )

    pool = get_model_pool()
    if ref_data_uris:
        resp, _used = await pool.chat_with_images(
            system=system, user=user_text, image_uris=ref_data_uris,
            reasoning_effort="close",
        )
    else:
        resp, _used = await pool.chat(
            system=system, user=user_text, temperature=0.3,
            reasoning_effort="close",
        )
    final_prompt = (resp.content or "").strip()
    if not final_prompt:
        raise RuntimeError("LLM 返回空内容")
    return final_prompt + PROMPT_REF_FIX_SUFFIX


async def _mq_async_generate(prompt: str, model: str, num_output: int,
                             ref_uris: list[str], session_id: str) -> list[str]:
    """独立实现(参考老 generate 端点):批量生图 N 张,结果落盘,返回 storage_uri 列表。"""
    from wellflow.app.llm.factory import get_llm_client
    from wellflow.app.utils.image_store import paths_to_data_uris, save_output_image
    from wellflow.app.utils.async_task_lib import download_image

    num_output = max(1, min(num_output, 6))
    chain = _mq_model_chain(model.strip())
    ref_data_uris = paths_to_data_uris(ref_uris) if ref_uris else None
    sem = asyncio.Semaphore(settings.mannequins_gen_concurrency)

    async def _one(i: int):
        errors = []
        async with sem:
            for backend_model in chain:
                try:
                    client = get_llm_client("image", model_override=backend_model)
                    r = await client.generate_image(
                        prompt=prompt,
                        image_uris=ref_data_uris,
                        size="1024x1536",
                        n=1,
                        response_format="b64_json",
                    )
                    img = r.all_images[0]
                    b64 = img.b64_json or (img.url or "")
                    if not b64:
                        raise RuntimeError(f"{backend_model}: AI 未返回图片结果")
                    if b64.startswith("http"):
                        b64 = await asyncio.to_thread(download_image, b64, backend_model)
                    data_uri = f"data:image/png;base64,{b64}"
                    return i, await asyncio.to_thread(
                        save_output_image, session_id, f"gen-{i}", data_uri)
                except Exception as e:
                    errors.append(f"{backend_model}: {str(e)[:80]}")
        raise RuntimeError("；".join(errors))

    results = await asyncio.gather(*(_one(i + 1) for i in range(num_output)),
                                   return_exceptions=True)
    uris: list[str] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        uris.append(r[1])
    if not uris:
        raise RuntimeError("全部生图失败")
    return uris


async def _mq_async_fine_tune(image_uri: str, prompt: str, model: str,
                              session_id: str) -> str:
    """独立实现(参考老 fine_tune 端点):目标图 → 单张图生图,落盘,返回 storage_uri。"""
    from wellflow.app.llm.factory import get_llm_client
    from wellflow.app.utils.image_store import paths_to_data_uris, save_output_image
    from wellflow.app.utils.async_task_lib import download_image

    chain = _mq_model_chain(model.strip())
    target_uris = paths_to_data_uris([image_uri])
    errors = []
    for backend_model in chain:
        try:
            client = get_llm_client("image", model_override=backend_model)
            r = await client.generate_image(
                prompt=prompt,
                image_uris=target_uris,
                size="1024x1536",
                n=1,
                response_format="b64_json",
            )
            img = r.all_images[0]
            b64 = img.b64_json or (img.url or "")
            if not b64:
                raise RuntimeError(f"{backend_model}: AI 未返回图片结果")
            if b64.startswith("http"):
                b64 = await asyncio.to_thread(download_image, b64, backend_model)
            data_uri = f"data:image/png;base64,{b64}"
            return await asyncio.to_thread(save_output_image, session_id, "tuned", data_uri)
        except Exception as e:
            errors.append(f"{backend_model}: {str(e)[:80]}")
    raise RuntimeError("；".join(errors))


async def _run_async_step(task: dict) -> None:
    """生成路径异步步骤执行器(按 task_type 分发;复用共享任务设施)。"""
    store = _mq_task_store()
    from wellflow.app.utils.async_task_lib import spawn_heartbeat
    spawn_heartbeat(store, task["task_id"])

    async def fail(err: str) -> None:
        task["status"] = "failed"
        task["error"] = err
        await asyncio.to_thread(store.save, task)
        if task.get("mannequin_id"):
            await asyncio.to_thread(_mq_mark_failed_sync, int(task["mannequin_id"]))

    async with _MQ_SEMAPHORE:
        try:
            task["status"] = "processing"
            await asyncio.to_thread(store.save, task)
            t = task["task_type"]
            if t == "optimize":
                task["final_prompt"] = await _mq_async_optimize(
                    task["raw_prompt"], task["tags"], task["ref_uris"])
            elif t == "generate":
                uris = await _mq_async_generate(
                    task["prompt"], task["generate_model"],
                    task["num_output"], task["ref_uris"], task["session_id"])
                task["images"] = [
                    {"index": i + 1, "storage_uri": u, "url": "/" + u.lstrip("/")}
                    for i, u in enumerate(uris)
                ]
                # ── 写回行(cover=第一张、转 active)+ generate_log(output_uris 存 N 张图) ──
                from wellflow.app.database import AsyncSessionLocal as _ASL2
                from wellflow.app.models.mannequin_models import (
                    Mannequin as _M2, MannequinGenerateLog as _ML2,
                )
                async with _ASL2() as db:
                    mm = await db.get(_M2, int(task["mannequin_id"])) if task.get("mannequin_id") else None
                    if mm is None:
                        raise RuntimeError(f"模特 {task.get('mannequin_id')} 已被删除")
                    mm.cover_storage_uri = uris[0]
                    mm.generate_model = task["generate_model"]
                    mm.generate_prompt = task["prompt"]
                    mm.status = "active"
                    db.add(_ML2(
                        mannequin_id=mm.id,
                        round_type="first_round",
                        input_desc=task["prompt"],
                        final_prompt=task["prompt"],
                        generate_model=task["generate_model"],
                        num_output=task["num_output"],
                        output_uris=uris,
                        status="success",
                    ))
                    await db.commit()
            elif t == "fine_tune":
                task["image_uri"] = await _mq_async_fine_tune(
                    task["input_uri"], task["prompt"], task["generate_model"],
                    task["session_id"])
                # ── 写回(传了 mannequin_id 时):行 cover=新图、转 active;generate_log 记微调轮 ──
                if task.get("mannequin_id") is not None:
                    from wellflow.app.database import AsyncSessionLocal as _ASL3
                    from wellflow.app.models.mannequin_models import (
                        Mannequin as _M3, MannequinGenerateLog as _ML3,
                    )
                    async with _ASL3() as db:
                        mm = await db.get(_M3, int(task["mannequin_id"]))
                        if mm is None:
                            raise RuntimeError(f"模特 {task['mannequin_id']} 已被删除")
                        mm.cover_storage_uri = task["image_uri"]
                        mm.status = "active"
                        db.add(_ML3(
                            mannequin_id=mm.id,
                            round_type="fine_tune",
                            target_image_uri=task["input_uri"],
                            input_desc=task["prompt"],
                            final_prompt=task["prompt"],
                            generate_model=task["generate_model"],
                            num_output=1,
                            output_uris=[task["image_uri"]],
                            status="success",
                        ))
                        await db.commit()
            elif t == "auto_tag":
                vtags, desc, suggested = await _mq_auto_tag(
                    task["data_uris"], task.get("extra_context"))
                task["tags"] = vtags
                task["description"] = desc
                task["suggested_name"] = suggested
            task["status"] = "done"
            task["error"] = ""
            await asyncio.to_thread(store.save, task)
        except Exception as e:
            await fail(f"{t}失败: {str(e)[:400]}")


async def _spawn_async_step(task: dict) -> dict:
    """建任务文件 → 起后台协程 → 返回 {task_id, status}。"""
    import os as _os
    store = _mq_task_store()
    task["pid"] = str(_os.getpid())
    await asyncio.to_thread(store.save, task)
    _bg = asyncio.create_task(_run_async_step(task))
    _MQ_BG_TASKS.add(_bg)
    _bg.add_done_callback(_MQ_BG_TASKS.discard)
    return {"task_id": task["task_id"], "status": "processing"}


@router.post(
    "/async/optimize-prompt",
    response_model=StandardResponse[dict],
    summary="异步优化提示词(秒回 task_id,后台 VLM,轮询 ai-status 拿 final_prompt)",
    tags=["模特创建流程 · 异步入口"],
)
async def async_optimize_prompt(
    raw_prompt: str = Form(...),
    tags: str | None = Form(None),
    ref_images: list[UploadFile] = File(default_factory=list),
):
    import uuid as _uuid
    import time as _time
    raw_refs = [(f.filename or "ref", await f.read(), f.content_type) for f in ref_images]
    ref_uris = await asyncio.to_thread(_mq_save_refs, raw_refs)
    task_id = f"{int(_time.time() * 1000)}_{_uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "task_type": "optimize", "status": "processing",
        "step": "queued", "raw_prompt": raw_prompt, "tags": tags,
        "ref_uris": ref_uris, "error": "",
    }
    return ok(await _spawn_async_step(task))


@router.post(
    "/async/generate",
    response_model=StandardResponse[dict],
    summary="异步批量生图(秒回 task_id,后台生图 N 张并落盘,轮询拿 images)",
    tags=["模特创建流程 · 异步入口"],
)
async def async_generate_images(
    prompt: str = Form(...),
    generate_model: str = Form("qwen-image-3.0"),
    num_output: int = Form(3),
    name: str | None = Form(None),
    ref_images: list[UploadFile] = File(default_factory=list),
):
    """v3.1:点击「生成模特」即建行(status=generating,列表立刻可见转圈),
    后台生成 N 张图落盘后写回行(cover=第一张、转 active)并写 generate_log。
    """
    import uuid as _uuid
    import time as _time
    from datetime import datetime as _dt
    from sqlalchemy.exc import IntegrityError
    from wellflow.app.database import AsyncSessionLocal as _ASL
    from wellflow.app.models.mannequin_models import Mannequin as _M

    raw_refs = [(f.filename or "ref", await f.read(), f.content_type) for f in ref_images]
    ref_uris = await asyncio.to_thread(_mq_save_refs, raw_refs)

    # ── 点击即建行(列表立刻可见「正在生成」;撞号重试一次) ──
    tmp_name = (name or f"AI模特{_dt.now():%m%d-%H%M}").strip()
    m = None
    last_err: Exception | None = None
    for _attempt in range(2):
        try:
            async with _ASL() as db:
                m = _M(
                    mannequin_no=await _mq_gen_no(db),
                    name=tmp_name,
                    scope="mine",
                    origin="ai_generate",
                    status="generating",
                )
                db.add(m)
                await db.commit()
            break
        except IntegrityError:
            last_err = IntegrityError("mannequin_no 冲突")
    if m is None:
        raise HTTPException(400, f"创建失败: {last_err}")

    task_id = f"{int(_time.time() * 1000)}_{_uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "task_type": "generate", "status": "processing",
        "step": "queued", "prompt": prompt,
        "generate_model": generate_model.strip(),
        "num_output": max(1, min(num_output, 6)),
        "ref_uris": ref_uris, "session_id": task_id,
        "mannequin_id": m.id, "error": "",
    }
    _resp = await _spawn_async_step(task)
    return ok({
        "mannequin_id": m.id, "mannequin_no": m.mannequin_no,
        "task_id": task_id, "status": "generating",
    })


@router.post(
    "/async/fine-tune",
    response_model=StandardResponse[dict],
    summary="异步单张微调(秒回 task_id,后台图生图并落盘,轮询拿新图)",
    tags=["模特创建流程 · 异步入口"],
)
async def async_fine_tune(
    image_uri: str = Form(...),
    prompt: str = Form(...),
    generate_model: str = Form("qwen-image-3.0"),
    mannequin_id: int | None = Form(None),
):
    """v3.2:传 mannequin_id 时,行置 generating(列表转圈「微调中」),
    完成后行 cover 更新为微调新图并写 generate_log(fine_tune 轮)。
    """
    import uuid as _uuid
    import time as _time
    from wellflow.app.database import AsyncSessionLocal as _ASL
    from wellflow.app.models.mannequin_models import Mannequin as _M

    # ── 行置 generating(列表立刻可见「处理中」) ──
    if mannequin_id is not None:
        async with _ASL() as db:
            mm = await db.get(_M, mannequin_id)
            if mm is None:
                raise HTTPException(404, f"模特 {mannequin_id} 不存在")
            mm.status = "generating"
            await db.commit()

    task_id = f"{int(_time.time() * 1000)}_{_uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "task_type": "fine_tune", "status": "processing",
        "step": "queued", "input_uri": image_uri, "prompt": prompt,
        "generate_model": generate_model.strip(),
        "session_id": task_id, "mannequin_id": mannequin_id, "error": "",
    }
    _resp = await _spawn_async_step(task)
    return ok({
        "task_id": task_id, "status": "processing",
        **({"mannequin_id": mannequin_id} if mannequin_id is not None else {}),
    })


@router.post(
    "/async/auto-tag",
    response_model=StandardResponse[dict],
    summary="异步识别入库资料(秒回 task_id,后台 VLM 打标,轮询拿 tags/description)",
    tags=["模特创建流程 · 异步入口"],
)
async def async_auto_tag(
    files: list[UploadFile] = File(...),
    extra_context: str | None = Form(None),
):
    import uuid as _uuid
    import time as _time
    raw_refs = [(f.filename or "img", await f.read(), f.content_type) for f in files[:1]]
    ref_uris = await asyncio.to_thread(_mq_save_refs, raw_refs)
    from wellflow.app.utils.image_store import paths_to_data_uris
    data_uris = await asyncio.to_thread(paths_to_data_uris, ref_uris)
    task_id = f"{int(_time.time() * 1000)}_{_uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "task_type": "auto_tag", "status": "processing",
        "step": "queued", "data_uris": data_uris,
        "extra_context": extra_context, "error": "",
    }
    return ok(await _spawn_async_step(task))


@router.get(
    "/{mannequin_id}/generate-logs",
    response_model=StandardResponse[dict],
    summary="生成记录(异步生成路径的 N 张图;只读,刷新页面后恢复展示用)",
    tags=["模特创建流程 · 异步入口"],
)
async def mannequin_generate_logs(mannequin_id: int):
    from sqlalchemy import select as _select
    from wellflow.app.database import AsyncSessionLocal as _ASL
    from wellflow.app.models.mannequin_models import MannequinGenerateLog as _ML
    async with _ASL() as db:
        rows = (await db.execute(
            _select(_ML).where(_ML.mannequin_id == mannequin_id).order_by(_ML.id)
        )).scalars().all()
        logs = [
            {
                "id": r.id,
                "round_type": r.round_type,
                "target_image_uri": r.target_image_uri,
                "input_desc": r.input_desc,
                "generate_model": r.generate_model,
                "num_output": r.num_output,
                "output_uris": r.output_uris or [],
                "output_urls": ["/" + u.lstrip("/") for u in (r.output_uris or [])],
                "status": r.status,
                "created_at": to_cn_iso(r.created_at),
            }
            for r in rows
        ]
    return ok({"logs": logs})
