# -*- coding: utf-8 -*-
"""穿搭库 API(参照 mannequins.py 规范:四层结构 + StandardResponse)。

异步主线(先入库、后补全,列表全程可见,用户任意时刻可离开可接上):
  0. 上传      POST /api/wellflow/image/uploads?session_id=xxx(同事统一接口)
  1. 拆解      POST /api/outfit/ai-extract(点击即入库 extracting → 后台识别+抠图
               → pending_select 待选件)
  2. 生成      POST /api/outfit/generate(选件后带 outfit_id 更新行 → generating
               → 后台平铺+打标 → pending_confirm 待确认,预填 VLM 名称/标签/描述)
  3. 确认入库  PUT /api/outfit/{id}(用户改名称/标签/描述后带 status=active)
  轮询        GET /api/outfit/ai-status?task_id=xxx(拆解/生成任务通用)
  状态机:extracting → pending_select → generating → pending_confirm → active;
          任一步失败 → failed(可删除重来)

老交互端点(保留,行为不变):flatlay 平铺 / auto-tag 打标 / POST /api/outfit 手动入库
  + 列表 GET /api/outfit / 详情 GET /{id} / 编辑 PUT / 删除 DELETE
"""

from __future__ import annotations

import asyncio
import base64
import json as json_mod
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wellflow.app.api.utils import ok, to_cn_iso
from wellflow.app.config import settings
from wellflow.app.database import get_db, get_db_async, session_scope, AsyncSessionLocal
from wellflow.app.llm.factory import get_llm_client
from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.repositories.outfit_repo import OutfitRepo
from wellflow.app.schemas.outfit_schemas import (
    OutfitAutoTagRequest, OutfitAutoTagResponse,
    OutfitCreateRequest, OutfitUpdateRequest, OutfitExtractRequest,
    OutfitFlatlayRequest, OutfitGenerateRequest,
    OutfitDetailResponse, OutfitListItem, OutfitListResponse,
)

router = APIRouter(prefix="/outfit", tags=["穿搭库"])

REPO_ROOT = Path(__file__).resolve().parents[3]          # 仓库根(api/ -> app/ -> wellflow/ -> 根)
TASK_DIR = REPO_ROOT / "static" / "outfit_ai"            # 拆解任务状态文件(已 gitignore)
DEMO_ITEM_DIR = REPO_ROOT / "static" / "assets" / "outfit-demo"
SAMPLE_PHOTO = DEMO_ITEM_DIR / "original.png"

# ---------------------------------------------------------------------------
# 穿搭六组维度枚举(与 PM demo 口径一致;auto-tag 校验用)
# ---------------------------------------------------------------------------

COLOR_BASIC = ["黑", "白", "灰", "米", "米白", "棕", "卡其", "藏青", "军绿", "大地色"]
COLOR_COLORFUL = ["红", "橙", "黄", "绿", "蓝", "紫", "粉"]

OUTFIT_DIMENSION_GROUPS: dict[str, dict[str, Any]] = {
    "outfitStyle": {
        "label": "穿搭风格",
        "options": ["极简", "通勤", "休闲", "运动", "户外", "机能",
                    "潮流", "亲子", "可爱", "高级"],
    },
    "category": {
        "label": "品类",
        "options": ["羽绒服", "冲锋衣", "抓绒", "软壳", "T恤", "卫衣", "衬衫",
                    "裤子", "裙子", "内衣", "婴儿服", "家居服", "鞋", "包", "配饰"],
    },
    "color": {
        "label": "颜色",
        "groups": [
            {"label": "基础色", "options": COLOR_BASIC},
            {"label": "彩色", "options": COLOR_COLORFUL},
        ],
    },
    "material": {
        "label": "材质",
        "options": ["羽绒", "尼龙", "聚酯", "棉", "羊毛", "羊绒", "针织",
                    "抓绒", "防水面料", "冲锋衣面料", "皮革"],
    },
    "fit": {
        "label": "版型",
        "options": ["修身", "合身", "宽松", "Oversize", "短款", "中长款", "长款"],
    },
    "func": {
        "label": "功能属性",
        "options": ["防水", "防风", "保暖", "透气", "轻量",
                    "速干", "防晒", "防寒", "户外机能"],
    },
}

# 抠图链/上限/并发已迁移到 settings(outfit_extract_models / outfit_max_items /
# outfit_extract_concurrency,见 config.py)—— 2026-09-20 qwen-image-3.0 置于链首
_TASK_LOCK = threading.RLock()

# 生成流水线并发闸:保护公司网关,排队不拒绝(技术主管要求可同时开多个,但限制同时在跑)
GLOBAL_PIPELINE_CONCURRENCY = 3
_PIPELINE_ASYNC_SEMAPHORE = asyncio.Semaphore(GLOBAL_PIPELINE_CONCURRENCY)
_PID = str(os.getpid())                 # 任务文件判死用:重启后 pid 变化即视为中断

# 后台协程任务集合:持有引用防 GC(asyncio.create_task 无引用会被回收)
_BG_TASKS: set = set()

# 异步流程状态机(列表全程可见,用户任意时刻可离开、可从列表回来接上):
#   extracting(拆解中)→ pending_select(待选件)→ generating(生成中)→ pending_confirm(待确认)→ active
#   任一步失败 → failed(可删除重来)。非终态集合供降级守卫用(active 不可降级)。
OUTFIT_NON_TERMINAL = {"extracting", "pending_select", "generating", "pending_confirm"}

# 任务文件生命周期:TTL 1 天;同 pid 处理中超 1 小时判死(慢任务兜底——
# 单件抠图降级链最坏 27 分钟,短阈值会误杀还在正常跑的任务)
TASK_TTL_SECONDS = 24 * 3600
PROCESSING_STALE_SECONDS = 60 * 60

# 演示模式 6 件示例单品(与 PM demo 一致)
DEMO_ITEMS = [
    {"id": "jacket", "name": "军绿衬衫外套", "category": "衬衫", "color": "军绿"},
    {"id": "tee", "name": "米白圆领内搭", "category": "T恤", "color": "米白"},
    {"id": "trousers", "name": "炭灰直筒长裤", "category": "裤子", "color": "灰"},
    {"id": "shoes", "name": "白色低帮运动鞋", "category": "鞋", "color": "白"},
    {"id": "belt", "name": "黑色银扣腰带", "category": "配饰", "color": "黑"},
    {"id": "sunglasses", "name": "黑色方框墨镜", "category": "配饰", "color": "黑"},
]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _storage_uri_url(uri: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith("http"):
        return uri
    return "/" + uri.lstrip("/")


def _to_data_uris(uris: list[str]) -> list[str]:
    """storage_uri 列表 → data URI 列表(http 直链原样透传)。"""
    from wellflow.app.utils.image_store import paths_to_data_uris
    local = [u for u in uris if u and not u.startswith(("http://", "https://", "data:"))]
    passthrough = [u for u in uris if u and u.startswith(("http://", "https://", "data:"))]
    return paths_to_data_uris(local) + passthrough


def _resolve_local_uri(uri: str) -> Path | None:
    """storage_uri → 绝对路径。

    - uploads/... 相对路径 → 基于 upload_dir(wellflow/uploads)
    - /static/... 等以 / 开头 → 基于仓库根
    - http(s) → None(调用方自行处理)
    """
    if uri.startswith(("http://", "https://")):
        return None
    if uri.startswith("uploads/"):
        return Path(settings.upload_dir).resolve() / uri[len("uploads/"):]
    return REPO_ROOT / uri.lstrip("/")


def _task_path(task_id: str) -> Path:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    return TASK_DIR / f"{task_id}.json"


def _save_task(task: dict):
    with _TASK_LOCK:
        _task_path(task["task_id"]).write_text(
            json_mod.dumps(task, ensure_ascii=False), encoding="utf-8")


def _load_task(task_id: str):
    p = TASK_DIR / f"{task_id}.json"
    if p.exists():
        try:
            return json_mod.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _mark_outfit_status(outfit_id: int, status: str) -> None:
    """后台/sweep 线程里把穿搭行置为指定状态(行不存在或 DB 异常静默跳过)。

    降级为 failed 时,行已是 active 则跳过(防「UPDATE 成功后写任务文件 done 前
    线程被杀」把已完成的正式行误标 failed —— DB 行是唯一真相源)。
    """
    try:
        with session_scope() as db:
            o = OutfitRepo(db).get(outfit_id)
            if o is None:
                return
            if status == "failed" and o.status == "active":
                return
            o.status = status
            db.commit()
    except Exception:
        pass


async def _amark_outfit_status(outfit_id: int, status: str) -> None:
    """协程任务里把穿搭行置为指定状态(语义同 _mark_outfit_status,async 会话版)。"""
    try:
        async with AsyncSessionLocal() as db:
            o = await OutfitRepo(db).aget(outfit_id)
            if o is None:
                return
            if status == "failed" and o.status == "active":
                return
            o.status = status
            await db.commit()
    except Exception:
        pass


def _sweep_stale_tasks() -> None:
    """任务文件自愈(读时触发,不改 main.py):

    1. pid ≠ 当前进程 且 processing → 判死(服务重启中断)
    2. 同 pid 且 processing 超 PROCESSING_STALE_SECONDS → 判死(极端慢任务)
    3. mtime 超 TASK_TTL_SECONDS → 删除
    判死/删除前,若文件含 outfit_id 且 DB 行仍 generating → 行联动改 failed。
    """
    if not TASK_DIR.exists():
        return
    now = time.time()
    for p in TASK_DIR.glob("*.json"):
        try:
            t = json_mod.loads(p.read_text(encoding="utf-8"))
        except Exception:
            t = None
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        expired = now - mtime > TASK_TTL_SECONDS
        stale_processing = (
            isinstance(t, dict)
            and t.get("status") == "processing"
            and (t.get("pid") != _PID or now - mtime > PROCESSING_STALE_SECONDS)
        )
        if not expired and not stale_processing:
            continue
        outfit_id = (t or {}).get("outfit_id")
        if stale_processing:
            # 任务中断 → 任务文件判死 + 行联动 failed(仅此场景联动;
            # TTL 过期只删文件、不动行 —— 历史任务文件清理不能影响已完成的行)
            if outfit_id:
                _mark_outfit_status(int(outfit_id), "failed")
            t["status"] = "failed"
            t["error"] = "服务重启或任务超时中断,请重试"
            try:
                p.write_text(json_mod.dumps(t, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        else:
            try:
                p.unlink()
            except OSError:
                pass


def _extract_json(text: str) -> dict:
    if not text:
        return {}
    try:
        obj = json_mod.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except json_mod.JSONDecodeError:
        pass
    import re as _re
    fenced = _re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=_re.IGNORECASE)
    fenced = _re.sub(r"\s*```$", "", fenced)
    try:
        obj = json_mod.loads(fenced.strip())
        if isinstance(obj, dict):
            return obj
    except json_mod.JSONDecodeError:
        pass
    first = fenced.find("{")
    last = fenced.rfind("}")
    if first >= 0 and last > first:
        try:
            obj = json_mod.loads(fenced[first:last + 1])
            if isinstance(obj, dict):
                return obj
        except json_mod.JSONDecodeError:
            pass
    return {}


def _save_output(session_id: str, name: str, b64: str) -> str:
    """b64 → 落盘 uploads/{session_id}/outputs/{name}.png,返回 storage_uri。"""
    from wellflow.app.utils.image_store import save_output_image
    data_uri = f"data:image/png;base64,{b64}"
    return save_output_image(session_id, name, data_uri)


def _read_original_image(original_uri: str) -> bytes:
    """读取原图字节(http 直链 / 本地 storage_uri),>15MB 拒绝。"""
    if original_uri.startswith(("http://", "https://")):
        import urllib.request as _ur
        try:
            with _ur.urlopen(original_uri, timeout=20) as resp:
                raw = resp.read()
        except Exception:
            raise HTTPException(400, "原图链接无法打开,请先走统一上传")
    else:
        p = _resolve_local_uri(original_uri)
        if not p or not p.exists():
            raise HTTPException(400, f"原图不存在: {original_uri}")
        raw = p.read_bytes()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(400, "图片过大(>15MB)")
    return raw


# ---------------------------------------------------------------------------
# CRUD(入库 + 列表/详情/编辑/删除)
# ---------------------------------------------------------------------------

def _to_list_item(o) -> OutfitListItem:
    return OutfitListItem(
        id=o.id, outfit_no=o.outfit_no, name=o.name, desc=o.desc, tags=o.tags,
        scope=o.scope, origin=o.origin, status=o.status,
        cover_storage_uri=o.cover_storage_uri,
        cover_url=_storage_uri_url(o.cover_storage_uri),
        original_storage_uri=o.original_storage_uri,
        original_url=_storage_uri_url(o.original_storage_uri),
        created_at=to_cn_iso(o.created_at), updated_at=to_cn_iso(o.updated_at),
    )


def _to_detail(o) -> OutfitDetailResponse:
    return OutfitDetailResponse(
        id=o.id, outfit_no=o.outfit_no, name=o.name, desc=o.desc, tags=o.tags,
        scope=o.scope, origin=o.origin, status=o.status,
        cover_storage_uri=o.cover_storage_uri, cover_url=_storage_uri_url(o.cover_storage_uri),
        original_storage_uri=o.original_storage_uri,
        original_url=_storage_uri_url(o.original_storage_uri),
        items=o.items or [], dims=o.dims or {},
        created_at=to_cn_iso(o.created_at), updated_at=to_cn_iso(o.updated_at),
    )


@router.get("/dimensions", summary="穿搭六组维度枚举(颜色含分组)")
async def get_dimensions():
    return ok({"groups": OUTFIT_DIMENSION_GROUPS})


@router.get("/ai-status", summary="轮询拆解/生成任务状态")
async def ai_status(task_id: str):
    await run_in_threadpool(_sweep_stale_tasks)
    t = await asyncio.to_thread(_load_task, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return ok(t)


# ---------------------------------------------------------------------------
# 交互端点 2:平铺总图合成(PIL,本地)
# ---------------------------------------------------------------------------

FLATLAY_W, FLATLAY_H = 1448, 1086
FLATLAY_BG = (247, 245, 242)


def _layout_grid(n: int) -> tuple[int, int]:
    if n <= 1:
        return 1, 1
    if n <= 3:
        return n, 1
    if n == 4:
        return 2, 2
    return 3, 2




@router.get("", response_model=dict, summary="列出穿搭(scope/q/dims 筛选分页)")
async def list_outfits(
    scope: str | None = Query(default=None),
    q: str | None = Query(default=None),
    dims: str | None = Query(default=None, description='JSON:{"outfitStyle":["极简"]}'),
    page: int = Query(1),
    page_size: int = Query(20),
    db: AsyncSession = Depends(get_db_async),
):
    await run_in_threadpool(_sweep_stale_tasks)
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

    repo = OutfitRepo(db)
    items, total = await repo.alist(scope=scope, q=q, dims=parsed_dims, page=page, page_size=page_size)
    return ok(OutfitListResponse(
        items=[_to_list_item(o) for o in items],
        total=total, page=page, page_size=page_size,
    ).model_dump())


@router.get("/{outfit_id}", response_model=dict, summary="查询穿搭详情")
async def get_outfit(outfit_id: int, db: AsyncSession = Depends(get_db_async)):
    await run_in_threadpool(_sweep_stale_tasks)
    repo = OutfitRepo(db)
    o = await repo.aget(outfit_id)
    if not o:
        raise HTTPException(404, "穿搭不存在")
    return ok(_to_detail(o).model_dump())


@router.post("", response_model=dict, summary="确认入库(一次事务写 outfit 表)")
async def create_outfit(body: OutfitCreateRequest, db: AsyncSession = Depends(get_db_async)):
    """穿搭最终入库:交互端点产物全部通过 storage_uri 引用,此处一次事务落库。"""
    repo = OutfitRepo(db)
    try:
        o = await repo.acreate(
            name=body.name,
            desc=body.desc,
            tags=body.tags,
            scope=body.scope,
            origin=body.origin,
            cover_storage_uri=body.cover_storage_uri,
            original_storage_uri=body.original_storage_uri,
            items=[it.model_dump() for it in body.items] if body.items else None,
            dims=body.dims.model_dump() if body.dims else None,
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"创建失败: {e}")
    return ok(_to_detail(o).model_dump())


@router.put("/{outfit_id}", response_model=dict, summary="更新穿搭/确认入库/返回上一步(官方资产 403)")
async def update_outfit(outfit_id: int, body: OutfitUpdateRequest, db: AsyncSession = Depends(get_db_async)):
    """编辑穿搭;确认入库 = 待确认行提交 {name, desc, tags, dims, status:"active"};

    「返回上一步」= 待确认行提交 {status:"pending_select"}:清空生成产物
    (封面/标签/描述),保留 items/name,退回待选件重新勾选。
    """
    repo = OutfitRepo(db)
    o = await repo.aget(outfit_id)
    if not o:
        raise HTTPException(404, "穿搭不存在")
    if o.scope == "official":
        raise HTTPException(403, "官方资产为平台精选,禁止编辑")
    try:
        if body.status == "pending_select":
            # 确认页返回上一步:仅待确认行允许回退
            if o.status != "pending_confirm":
                raise HTTPException(400, f"当前状态 {o.status} 不允许返回上一步")
            o.cover_storage_uri = None
            o.dims = {}
            o.desc = None
            o.tags = None
            o.status = "pending_select"
            await db.commit()
        else:
            o = await repo.aupdate(
                outfit_id,
                name=body.name,
                desc=body.desc,
                tags=body.tags,
                cover_storage_uri=body.cover_storage_uri,
                original_storage_uri=body.original_storage_uri,
                items=[it.model_dump() for it in body.items] if body.items is not None else None,
                dims=body.dims.model_dump() if body.dims is not None else None,
                status=body.status,
            )
            await db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ok(_to_detail(o).model_dump())


@router.delete("/{outfit_id}", response_model=dict, summary="删除穿搭(官方资产 403)")
async def delete_outfit(outfit_id: int, db: AsyncSession = Depends(get_db_async)):
    repo = OutfitRepo(db)
    o = await repo.aget(outfit_id)
    if not o:
        raise HTTPException(404, "穿搭不存在")
    if o.scope == "official":
        raise HTTPException(403, "官方资产为平台精选,禁止删除")
    if not await repo.adelete(outfit_id):
        raise HTTPException(404, "穿搭不存在")
    await db.commit()
    return ok({"deleted": True, "outfit_id": outfit_id})


# ---------------------------------------------------------------------------
# 交互端点 1:AI 拆解(异步任务 + 轮询;真实识别 + 逐件抠图)
# ---------------------------------------------------------------------------

async def _call_vlm_recognize(raw: bytes):
    """VLM(模型池)识别照片单品清单,≤ settings.outfit_max_items 件。

    模型池偶发返回空清单(同一张图重试即有结果)——空结果自动重试一次再判失败。
    """
    b64 = base64.b64encode(raw).decode()
    prompt = (
        "这是一张人物全身穿搭照片。请识别照片中人物身上穿/戴的每一件单品"
        "(上衣、裤装、鞋、包、腰带、眼镜等),输出 JSON:\n"
        '{"items":[{"name":"单品名(简洁,如 军绿衬衫外套)",'
        '"category":"品类(衬衫/T恤/裤子/鞋/包/配饰 等通用词)",'
        '"color":"颜色(简洁,如 军绿/米白/黑)"}]}\n'
        "要求:\n1. 按从上到下、从外到内排列\n"
        "2. 不要包含人物本身特征(发型、肤色、身材)\n3. 只输出 JSON,不要任何解释"
    )

    content = ""
    for _attempt in range(2):
        pool = get_model_pool()
        resp, _used_model = await pool.chat_with_images(
            system="你是专业的电商服饰单品识别专家。",
            user=prompt,
            image_uris=[f"data:image/png;base64,{b64}"],
            response_format={"type": "json_object"},
            reasoning_effort=settings.text_reasoning_effort,
        )
        content = resp.content or ""
        parsed = _extract_json(content)
        items = parsed.get("items")
        if not isinstance(items, list) or not items:
            continue  # 空清单/解析失败 → 再试一次
        norm = []
        for it in items[:settings.outfit_max_items]:
            if not isinstance(it, dict) or not it.get("name"):
                continue
            norm.append({
                "name": str(it["name"]).strip(),
                "category": str(it.get("category") or "").strip(),
                "color": str(it.get("color") or "").strip(),
            })
        if norm:
            return norm
    raise RuntimeError(f"VLM 未返回有效单品清单: {content[:200]}")
def _download_image(url: str, model: str) -> str:
    """下载网关返回的生成图 url → b64(校验图片魔数,防拿到错误页)。"""
    import urllib.request as _ur
    try:
        with _ur.urlopen(url, timeout=60) as resp:
            data = resp.read()
    except Exception as e:
        raise RuntimeError(f"{model}: 下载生成图失败: {str(e)[:100]}")
    if len(data) < 64 or data[:8] not in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"):
        raise RuntimeError(f"{model}: 下载内容不是图片({data[:16]!r})")
    return base64.b64encode(data).decode()


async def _extract_item_image(raw: bytes, name: str):
    """单件抠图:降级链依次尝试,成功返回 b64,全败抛 RuntimeError。

    网关可能只回 url 不回 b64(如 qwen-image-3.0):http url 下载校验后转 b64。
    """
    prompt = (
        f"提取这张穿搭照片中的「{name}」,生成干净的专业白底商品图。"
        "要求:只保留这一件单品,主体完整(被遮挡部分合理补全),"
        "背景纯白,居中构图,无阴影、无文字"
    )
    errors = []
    for model in settings.outfit_extract_models:
        try:
            client = get_llm_client("image", model_override=model)
            r = await client.generate_image(
                prompt=prompt,
                image_uris=[f"data:image/png;base64,{base64.b64encode(raw).decode()}"],
                size="1024x1024",
                n=1,
                response_format="b64_json",
            )
            img = r.all_images[0]
            b64 = img.b64_json or (img.url or "")
            if not b64:
                raise RuntimeError(f"{model}: AI 未返回图片结果")
            if b64.startswith("http"):
                b64 = await asyncio.to_thread(_download_image, b64, model)
            return b64
        except Exception as e:
            errors.append(f"{model}: {str(e)[:100]}")
    raise RuntimeError("；".join(errors))
async def _run_cutout(task: dict, raw: bytes, items: list[dict], session_id: str) -> tuple[int, dict]:
    """并发抠图(settings.outfit_extract_concurrency 路,asyncio 协程并发)。

    items:任务清单元素(含 id/name/category/color,storage_uri 由本函数回填)。
    返回 (成功件数, 失败件 {idx: error})。每件完成即落盘任务文件。
    """
    sem = asyncio.Semaphore(min(settings.outfit_extract_concurrency, len(items)))
    results: dict = {}
    errors: dict = {}

    async def worker(idx: int):
        b64 = None
        err = ""
        try:
            b64 = await _extract_item_image(raw, items[idx]["name"])
        except Exception as e:
            err = str(e)[:200]
        uri = ""
        if b64 is not None:
            uri = await asyncio.to_thread(
                _save_output, session_id, f"item-{idx:02d}", b64)
        if b64 is not None:
            results[idx] = b64
            task["items"][idx]["storage_uri"] = uri
        else:
            errors[idx] = err
            task["failed_items"].append({"name": items[idx]["name"], "error": err})
        task["progress"] = {"done": len(results) + len(errors), "total": len(items)}
        await asyncio.to_thread(_save_task, task)

    async def limited(idx: int):
        async with sem:
            await worker(idx)

    await asyncio.gather(*(limited(i) for i in range(len(items))))
    return len(results), errors
async def _run_extract(task: dict, raw: bytes, mode: str):
    """后台拆解任务(asyncio 协程):demo=预制示例;real=VLM 识别+并发抠图。

    完成 → 先 UPDATE 行(items + status=pending_select)再写任务文件 done(顺序钉死);
    失败 → 行 failed + 任务文件 failed。产物落 uploads/{sid}/outputs/。
    """
    sid = task.get("session_id") or "outfit"
    outfit_id = task.get("outfit_id")

    async def fail(err: str) -> None:
        task["status"] = "failed"
        task["error"] = err
        await asyncio.to_thread(_save_task, task)
        if outfit_id:
            await _amark_outfit_status(int(outfit_id), "failed")

    try:
        if mode == "real":
            try:
                recognized = await _call_vlm_recognize(raw)
            except Exception as e:
                await fail(f"单品识别失败: {str(e)[:400]}")
                return
            norm_items = [
                {"id": f"r{i + 1}", "name": it["name"], "category": it["category"],
                 "color": it["color"], "storage_uri": ""}
                for i, it in enumerate(recognized)
            ]
            task["items"] = norm_items
            task["progress"] = {"done": 0, "total": len(norm_items)}
            task["failed_items"] = []
            await asyncio.to_thread(_save_task, task)

            ok_n, cutout_errors = await _run_cutout(task, raw, norm_items, sid)
            if ok_n == 0:
                await fail(f"全部 {len(norm_items)} 件单品抠图失败: " + "；".join(cutout_errors.values())[:400])
                return
            task["error"] = (f"{len(cutout_errors)} 件单品抠图失败(详见 failed_items)" if cutout_errors else "")
        else:
            # demo:预制 6 件示例(图已内置,直接返回)
            task["items"] = [
                {**it, "storage_uri": f"/static/assets/outfit-demo/{it['id']}.png"}
                for it in DEMO_ITEMS
            ]
            task["progress"] = {"done": len(DEMO_ITEMS), "total": len(DEMO_ITEMS)}
            task["failed_items"] = []
            task["error"] = ""

        # 先 UPDATE 行(待选件)→ 再写任务文件 done
        items_db = [it for it in task["items"] if it.get("storage_uri")]
        try:
            async with AsyncSessionLocal() as db:
                o = await OutfitRepo(db).aget(int(outfit_id)) if outfit_id else None
                if o is None:
                    raise RuntimeError(f"穿搭 {outfit_id} 已被删除")
                o.items = items_db
                o.status = "pending_select"
                await db.commit()
        except Exception as e:
            await fail(f"拆解结果写入失败: {str(e)[:400]}")
            return

        task["status"] = "done"
        await asyncio.to_thread(_save_task, task)
    except Exception as e:
        await fail(str(e)[:600])
@router.post("/ai-extract", response_model=dict, summary="AI 拆解(点击即入库 extracting,后台识别+抠图;demo/real)")
async def ai_extract(body: OutfitExtractRequest, db: AsyncSession = Depends(get_db_async)):
    """拆解也走「先入库」:立即写 outfit 行(status=extracting),后台识别+抠图。

    完成 → 行补 items、status=pending_select(待选件);失败 → 行 failed。
    body.outfit_id 传了=对已有待选件行「重新拆解」(返回调整用),不新建。
    响应 {outfit_id, task_id},前端关弹窗回列表,轮询 ai-status 看进度。
    """

    raw = await run_in_threadpool(_read_original_image, body.original_uri)

    await run_in_threadpool(_sweep_stale_tasks)

    mode = body.mode or "real"  # 显式传 mode 优先;默认真实识别
    sid = body.session_id or f"outfit_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    repo = OutfitRepo(db)

    if body.outfit_id is not None:
        # 重新拆解:对已有待选件行清单品、转 extracting,不新建;
        # 支持换图(原图字段同步更新为新上传的图)
        obj = await repo.aget(body.outfit_id)
        if obj is None:
            raise HTTPException(404, f"穿搭 {body.outfit_id} 不存在")
        if obj.scope == "official":
            raise HTTPException(403, "官方资产为平台精选,禁止编辑")
        if obj.status != "pending_select":
            raise HTTPException(400, f"当前状态 {obj.status} 不允许重新拆解")
        obj.original_storage_uri = body.original_uri
        obj.items = None
        obj.status = "extracting"
        await db.commit()
    else:
        # 新建行(点击即入库 extracting);批量并发撞 outfit_no 时回滚重试一次
        tmp_name = f"AI穿搭{datetime.now():%m%d-%H%M}"
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                obj = await repo.acreate(
                    name=tmp_name,
                    scope="mine",
                    origin="ai",
                    original_storage_uri=body.original_uri,
                    items=None,
                    dims={},
                )
                obj.status = "extracting"  # DB 默认 active,必须显式覆盖
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
                last_err = IntegrityError("outfit_no 冲突")
        if obj is None:
            raise HTTPException(400, f"创建失败: {last_err}")

    task_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "task_type": "extract", "pid": _PID,
        "mode": mode, "status": "processing",
        "session_id": sid,
        "original_uri": body.original_uri,
        "outfit_id": obj.id,
        "items": [], "failed_items": [], "progress": {"done": 0, "total": 0}, "error": "",
    }
    await asyncio.to_thread(_save_task, task)
    _bg = asyncio.create_task(_run_extract(task, raw, mode))
    _BG_TASKS.add(_bg)
    _bg.add_done_callback(_BG_TASKS.discard)
    return ok({
        "outfit_id": obj.id, "outfit_no": obj.outfit_no,
        "task_id": task_id, "status": "extracting",
    })


def _compose_flatlay(item_uris: list[str]) -> bytes:
    """把单品 storage_uri 列表拼成 4:3 平铺总图,返回 PNG bytes。

    无效/不存在的本地图静默跳过;有效图为 0 时抛 ValueError(调用方判失败)。
    """
    from PIL import Image
    import io as _io

    opened = []
    for uri in item_uris:
        src = _resolve_local_uri(uri)
        if not src or not src.exists():
            continue
        opened.append(Image.open(src))

    if not opened:
        raise ValueError("没有可用的单品图")

    cols, rows = _layout_grid(len(opened))
    canvas = Image.new("RGB", (FLATLAY_W, FLATLAY_H), FLATLAY_BG)
    cell_w, cell_h = FLATLAY_W // cols, FLATLAY_H // rows
    padding = int(min(cell_w, cell_h) * 0.09)
    total = len(opened)
    full_rows, last_row_n = total // cols, total % cols
    idx = 0
    for row in range(rows):
        count_in_row = cols if row < full_rows else last_row_n
        if count_in_row == 0:
            break
        start_col = (cols - count_in_row) // 2
        for c in range(count_in_row):
            img = opened[idx].convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.alpha_composite(img)
            img = bg.convert("RGB")
            max_w, max_h = cell_w - padding * 2, cell_h - padding * 2
            ratio = min(max_w / img.width, max_h / img.height)
            if ratio < 1:
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
            x0 = (start_col + c) * cell_w + (cell_w - img.width) // 2
            y0 = row * cell_h + (cell_h - img.height) // 2
            canvas.paste(img, (x0, y0))
            idx += 1

    buf = _io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


@router.post("/flatlay", response_model=dict, summary="已选单品合成 4:3 平铺总图(输出落 outputs/)")
async def flatlay(body: OutfitFlatlayRequest):

    for uri in body.items:
        if uri.startswith(("http://", "https://")):
            raise HTTPException(400, f"单品图必须是本地 storage_uri: {uri}")
        src = _resolve_local_uri(uri)
        if not src or not src.exists():
            raise HTTPException(400, f"单品图不存在: {uri}")

    try:
        png_bytes = await run_in_threadpool(_compose_flatlay, body.items)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except ImportError:
        raise HTTPException(500, "服务端缺少 Pillow,无法合成平铺总图")

    sid = body.session_id or f"outfit_{uuid.uuid4().hex[:8]}"
    uri = _save_output(sid, f"flatlay-{int(time.time() * 1000)}", base64.b64encode(png_bytes).decode())
    return ok({"flatlay_storage_uri": uri, "flatlay_url": _storage_uri_url(uri), "session_id": sid})


# ---------------------------------------------------------------------------
# 交互端点 3:自动打标(仿 mannequins.auto_tag:VLM 读图 + 枚举校验防幻觉)
# ---------------------------------------------------------------------------

async def _do_auto_tag(
    data_uris: list[str], extra_context: str | None = None,
) -> tuple[dict[str, list[str]], str, str | None, str]:
    """VLM 读图打标(枚举校验防幻觉)。

    返回 (validated_dims, description, suggested_name, used_model)。
    VLM 返回非 JSON 抛 json.JSONDecodeError,其余异常原样上抛(调用方包装)。
    """
    dims_json = json_mod.dumps(OUTFIT_DIMENSION_GROUPS, ensure_ascii=False, indent=2)
    system = (
        "你是一个电商穿搭属性标注专家。根据提供的穿搭图(平铺总图或原图),"
        "从给定的维度枚举中选择最合适的值,以 JSON 格式返回。\n\n"
        "规则:\n1. 每个维度可多选(多值数组),也可以单选。\n"
        "2. 如果某维度无法从图中判断,返回空数组 []。\n"
        "3. 只使用枚举中出现的值,不要自己创造新值。\n"
        "4. 输出必须是一个合法的 JSON 对象,不要带 markdown 代码块标记或其他文字。"
    )
    user_text = (
        f"以下是维度枚举(JSON):\n{dims_json}\n\n"
        f"{'用户补充意图:' + extra_context if extra_context else ''}\n\n"
        "请为这张穿搭图打标,严格按以下 JSON 格式返回:\n"
        '{"dims": {"outfitStyle": [...], "category": [...], "color": [...], '
        '"material": [...], "fit": [...], "func": [...]}, '
        '"description": "一句话描述这套穿搭的风格气质", '
        '"suggested_name": "可选的穿搭名称(中文;无法建议可为 null)"}'
    )

    pool = get_model_pool()
    resp, used_model = await pool.chat_with_images(
        system=system, user=user_text, image_uris=data_uris,
        response_format={"type": "json_object"},
        reasoning_effort=settings.text_reasoning_effort,
    )
    content = (resp.content or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(l for l in lines if not l.startswith("```"))
    data = json_mod.loads(content)

    # 枚举校验:只保留合法值(防幻觉)
    raw_dims = data.get("dims", {}) if isinstance(data, dict) else {}
    validated: dict[str, list[str]] = {}
    for key, defn in OUTFIT_DIMENSION_GROUPS.items():
        allowed: set[str] = set()
        for grp in (defn.get("groups") or []):
            allowed.update(grp.get("options", []))
        allowed.update(defn.get("options") or [])
        vals = raw_dims.get(key, [])
        if isinstance(vals, list):
            cleaned = [str(v) for v in vals if str(v) in allowed]
            if cleaned:
                validated[key] = cleaned

    description = str(data.get("description", "")).strip()
    suggested = data.get("suggested_name")
    return validated, description, (suggested if isinstance(suggested, str) else None), used_model


@router.post("/auto-tag", response_model=dict, summary="入库前自动打标(六组维度+描述)")
async def auto_tag(body: OutfitAutoTagRequest):

    data_uris = _to_data_uris([body.image_uri])
    if not data_uris:
        raise HTTPException(400, f"无法读取 image_uri: {body.image_uri}")

    try:
        dims, description, suggested, used_model = await _do_auto_tag(
            data_uris, body.extra_context)
    except json_mod.JSONDecodeError as e:
        raise HTTPException(502, f"VLM 返回格式错误: {e}")
    except Exception as e:
        raise HTTPException(502, f"自动打标失败: {e}")

    return ok(OutfitAutoTagResponse(
        dims=dims,
        description=description,
        suggested_name=suggested,
        model=used_model,
    ).model_dump())


# ---------------------------------------------------------------------------
# 交互端点 4:一键异步生成(点击即入库 generating → 后台补全 → active/failed)
# ---------------------------------------------------------------------------

async def _run_generate(task: dict, raw: bytes, body) -> None:
    """后台补全流水线(asyncio 协程,并发闸内执行):平铺 → 打标 → UPDATE 行 → done。

    auto 模式多跑识别+抠图。顺序钉死:DB 行 UPDATE 成功后才写任务文件 done;
    完成后行 status=pending_confirm(名称/标签/描述预填 VLM 值,等用户确认入库);
    关键步骤失败 → 任务文件 failed + 行 status=failed(error 详情在任务文件)。
    """
    sid = task["session_id"]
    outfit_id = task["outfit_id"]

    async def fail(step_label: str, e: Exception) -> None:
        task["status"] = "failed"
        task["error"] = f"{step_label}失败: {str(e)[:400]}"
        await asyncio.to_thread(_save_task, task)
        await _amark_outfit_status(outfit_id, "failed")

    async with _PIPELINE_ASYNC_SEMAPHORE:
        try:
            # ── 1. 识别(auto 模式;items 模式清单已入库) ──
            if body.mode == "auto":
                task["step"] = "extract"
                try:
                    recognized = await _call_vlm_recognize(raw)
                except Exception as e:
                    await fail("extract", e)
                    return
                norm_items = [
                    {"id": f"r{i + 1}", "name": it["name"], "category": it["category"],
                     "color": it["color"], "storage_uri": ""}
                    for i, it in enumerate(recognized)
                ]
                task["items"] = norm_items
                task["progress"] = {"done": 0, "total": len(norm_items)}
                task["failed_items"] = []
                await asyncio.to_thread(_save_task, task)

                # ── 2. 抠图(auto 模式) ──
                task["step"] = "cutout"
                ok_n, _ = await _run_cutout(task, raw, norm_items, sid)
                if ok_n == 0 and task.get("failed_items"):
                    await fail("cutout", RuntimeError(f"全部 {len(norm_items)} 件单品抠图失败"))
                    return
            else:
                norm_items = task["items"]

            # ── 3. 平铺(零有效图判败,不产空白底图) ──
            task["step"] = "flatlay"
            try:
                item_uris = [it.get("storage_uri") for it in norm_items if it.get("storage_uri")]
                png_bytes = await asyncio.to_thread(_compose_flatlay, item_uris)
                flatlay_uri = await asyncio.to_thread(
                    _save_output, sid, f"flatlay-{int(time.time() * 1000)}",
                    base64.b64encode(png_bytes).decode())
            except Exception as e:
                await fail("flatlay", e)
                return
            task["flatlay_storage_uri"] = flatlay_uri

            # ── 4. 打标+描述 ──
            task["step"] = "tagging"
            try:
                dims, description, suggested, used_model = await _do_auto_tag(
                    await asyncio.to_thread(_to_data_uris, [flatlay_uri]), body.extra_context)
            except Exception as e:
                await fail("tagging", e)
                return
            task["dims"] = dims
            task["description"] = description
            task["suggested_name"] = suggested
            task["model"] = used_model

            # ── 5. 更新行(先 UPDATE 成功,再写 done;停在 pending_confirm 等用户确认入库) ──
            task["step"] = "saving"
            final_name = (body.name or suggested or f"AI穿搭{datetime.now():%m%d-%H%M}").strip()
            tags = ",".join(dict.fromkeys(v for vals in dims.values() for v in vals))[:512]
            items_db = [it for it in norm_items if it.get("storage_uri")]
            try:
                async with AsyncSessionLocal() as db:
                    o = await OutfitRepo(db).aget(outfit_id)
                    if o is None:
                        raise RuntimeError(f"穿搭 {outfit_id} 已被删除")
                    o.name = final_name
                    o.desc = description
                    o.tags = tags
                    o.cover_storage_uri = flatlay_uri
                    o.items = items_db
                    o.dims = dims
                    o.status = "pending_confirm"
                    await db.commit()
            except Exception as e:
                await fail("saving", e)
                return

            task["status"] = "done"
            task["error"] = ""
            await asyncio.to_thread(_save_task, task)
        except Exception as e:
            task["status"] = "failed"
            task["error"] = f"流水线异常: {str(e)[:400]}"
            await asyncio.to_thread(_save_task, task)
            await _amark_outfit_status(outfit_id, "failed")
@router.post("/generate", response_model=dict, summary="生成穿搭图(选件后:更新已有行;auto:新建行。后台平铺+打标)")
async def generate(body: OutfitGenerateRequest, db: AsyncSession = Depends(get_db_async)):
    """异步生成:行进入 generating,后台平铺+打标,完成后行 status=pending_confirm
    (预填 VLM 名称/标签/描述,等用户确认入库),失败 → 行 failed。

    outfit_id 传了=选件后生成:更新已有拆解行(必须 pending_select),不新建;
    不传=一键新建(auto 模式保留,前端不挂入口)。
    """
    raw = await run_in_threadpool(_read_original_image, body.original_uri)
    if not body.items and (body.mode == "items" or body.outfit_id is not None):
        raise HTTPException(400, "必须传 items 单品清单")

    await run_in_threadpool(_sweep_stale_tasks)

    sid = body.session_id or f"outfit_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    items_db = [it.model_dump() for it in body.items] if body.items else []
    repo = OutfitRepo(db)

    if body.outfit_id is not None:
        # 选件后生成:更新已有拆解行,不新建
        obj = await repo.aget(body.outfit_id)
        if obj is None:
            raise HTTPException(404, f"穿搭 {body.outfit_id} 不存在")
        if obj.scope == "official":
            raise HTTPException(403, "官方资产为平台精选,禁止编辑")
        if obj.status != "pending_select":
            raise HTTPException(400, f"当前状态 {obj.status} 不允许生成,请刷新列表")
        obj.items = items_db
        obj.status = "generating"
        await db.commit()
    else:
        # 一键新建(auto 模式;前端不挂入口)
        tmp_name = (body.name or f"AI穿搭{datetime.now():%m%d-%H%M}").strip()
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                obj = await repo.acreate(
                    name=tmp_name,
                    scope=body.scope,
                    origin="ai",
                    original_storage_uri=body.original_uri,
                    items=items_db or None,
                    dims={},
                )
                obj.status = "generating"  # DB 默认 active,必须显式覆盖
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
                last_err = IntegrityError("outfit_no 冲突")
        if obj is None:
            raise HTTPException(400, f"创建失败: {last_err}")

    task_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "task_type": "generate", "pid": _PID,
        "mode": body.mode, "status": "processing", "step": "queued",
        "session_id": sid, "original_uri": body.original_uri,
        "outfit_id": obj.id,
        "items": items_db, "failed_items": [], "progress": {"done": 0, "total": 0},
        "error": "", "requested_name": body.name,
    }
    await asyncio.to_thread(_save_task, task)
    _bg = asyncio.create_task(_run_generate(task, raw, body))
    _BG_TASKS.add(_bg)
    _bg.add_done_callback(_BG_TASKS.discard)
    return ok({
        "outfit_id": obj.id, "outfit_no": obj.outfit_no,
        "task_id": task_id, "status": "generating",
    })
