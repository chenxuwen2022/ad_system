# -*- coding: utf-8 -*-
"""场景库 API(仿 outfit.py 的 asyncio 终态:四层全 async + 先入库后补全)。

异步主线(点击即入库,列表全程可见):
  0. 上传      POST /api/wellflow/image/uploads?session_id=xxx(同事统一接口)
  1. AI 处理   POST /api/scene/ai-extract(点击即入库 extracting → 后台三步:
               ① 违规审核 ② 场景提取(生图) ③ 打标+一句话描述 → pending_confirm)
  2. 确认入库  PUT /api/scene/{id}(用户改名称/标签/描述后带 status=pending_review)
  3. 审核      PUT /api/scene/{id}(status=approved/returned)
  4. 重新整理  PUT /api/scene/{id}(status=extracting,后台重跑 AI 三步)
  轮询        GET /api/scene/ai-status?task_id=xxx
  状态机:extracting → pending_confirm → pending_review → approved / returned;
          任一步失败(含违规拦截)→ failed(可删除重来)
"""

from __future__ import annotations

import asyncio
import base64
import json as json_mod
import os
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
from wellflow.app.database import get_db_async, session_scope, AsyncSessionLocal
from wellflow.app.utils.async_task_lib import BackgroundTasks, TaskStore, download_image
from wellflow.app.llm.factory import get_llm_client
from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.repositories.scene_repo import SceneRepo
from wellflow.app.schemas.scene_schemas import (
    SceneExtractRequest, SceneUpdateRequest,
    SceneDetailResponse, SceneListItem, SceneListResponse, SceneDims,
)

router = APIRouter(prefix="/scene", tags=["场景库"])

REPO_ROOT = Path(__file__).resolve().parents[3]          # 仓库根
TASK_DIR = REPO_ROOT / "static" / "scene_ai"             # 处理任务状态文件(已 gitignore)

# ---------------------------------------------------------------------------
# 场景五维标签枚举(与 demo 口径一致;打标校验用)
# ---------------------------------------------------------------------------

SCENE_DIMENSION_GROUPS: dict[str, list[str]] = {
    "space": ["纯色棚拍", "摄影棚", "客厅", "卧室", "厨房", "卫生间", "办公室", "商场",
              "街道", "公园", "体育场", "健身房", "山林", "雪山", "沙滩", "营地", "岩壁"],
    "region": ["城市", "城市街头", "郊外", "森林", "山地", "雪地", "湖泊", "海边", "沙漠"],
    "season": ["春", "夏", "秋", "冬"],
    "weather": ["晴天", "阴天", "雨天", "雪天", "雾", "日落", "夜晚"],
    "sceneStyle": ["极简", "高级", "自然", "温馨", "生活化", "户外", "专业",
                   "科技", "潮流", "轻奢", "都市"],
}

# 处理流水线并发闸:保护公司网关,排队不拒绝
GLOBAL_PIPELINE_CONCURRENCY = 3
_PIPELINE_ASYNC_SEMAPHORE = asyncio.Semaphore(GLOBAL_PIPELINE_CONCURRENCY)
_PID = str(os.getpid())                 # 任务文件判死用

def _task_store() -> TaskStore:
    """惰性单例:任务文件存储(共享库)。"""
    return TaskStore(TASK_DIR, row_id_field="scene_id", mark_row_failed=_mark_scene_status)


def _bg_tasks() -> BackgroundTasks:
    return _BG_TASKS


_BG_TASKS = BackgroundTasks()

# 异步流程状态机:extracting → pending_confirm → pending_review → approved/returned;
# 任一步失败 → failed。非终态集合供降级守卫用(approved 不可降级)。
SCENE_NON_TERMINAL = {"extracting", "pending_confirm", "pending_review"}

# 任务文件生命周期:TTL 1 天;同 pid 处理中超 1 小时判死
TASK_TTL_SECONDS = 24 * 3600
PROCESSING_STALE_SECONDS = 60 * 60



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
    if uri.startswith(("http://", "https://")):
        return None
    if uri.startswith("uploads/"):
        return Path(settings.upload_dir).resolve() / uri[len("uploads/"):]
    return REPO_ROOT / uri.lstrip("/")


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


def _mark_scene_status(scene_id: int, status: str) -> None:
    """sweep 线程里把场景行置为指定状态(行不存在或 DB 异常静默跳过)。

    降级为 failed 时,行已是 approved 则跳过(DB 行是唯一真相源)。
    """
    try:
        with session_scope() as db:
            from sqlalchemy.orm import Session as _SyncSession
            o = _load_scene_sync(db, scene_id)
            if o is None:
                return
            if status == "failed" and o.status == "approved":
                return
            o.status = status
            db.commit()
    except Exception:
        pass


def _load_scene_sync(db, scene_id: int):
    """sweep(同步线程池)里取场景行:直接 ORM 查询,不依赖 async 会话。"""
    from wellflow.app.models.scene_models import Scene as _Scene
    return db.get(_Scene, scene_id)


async def _amark_scene_status(scene_id: int, status: str) -> None:
    """协程任务里把场景行置为指定状态(语义同 _mark_scene_status,async 会话版)。"""
    try:
        async with AsyncSessionLocal() as db:
            o = await SceneRepo(db).aget(scene_id)
            if o is None:
                return
            if status == "failed" and o.status == "approved":
                return
            o.status = status
            await db.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# AI 三步(协程)
# ---------------------------------------------------------------------------

async def _check_violation(raw: bytes) -> tuple[bool, str]:
    """① 违规审核(VLM):黄赌毒+暴力等,返回 (是否违规, 原因)。"""
    b64 = base64.b64encode(raw).decode()
    prompt = (
        "判断这张图片是否包含违规内容(色情、赌博、毒品、暴力血腥等)。"
        '输出 JSON:{"violation": true/false, "reason": "违规原因(未违规则为空字符串)"}。'
        "只输出 JSON,不要任何解释"
    )
    pool = get_model_pool()
    resp, _used_model = await pool.chat_with_images(
        system="你是电商内容安全审核专家,判断必须严格准确。",
        user=prompt,
        image_uris=[f"data:image/png;base64,{b64}"],
        response_format={"type": "json_object"},
        reasoning_effort="close",
    )
    data = _extract_json(resp.content or "")
    violation = bool(data.get("violation")) if isinstance(data, dict) else False
    reason = str(data.get("reason") or "") if isinstance(data, dict) else ""
    return violation, reason


async def _extract_scene_image(raw: bytes) -> str:
    """② 场景提取(生图降级链):去人物/物品只留场景,返回 b64。"""
    prompt = (
        "移除这张照片中的全部人物，包括人影、人物倒影和随身物品，"
        "用周围的环境内容自然填充被移除区域：延续原有的地形、植被、天空、水面、"
        "建筑与纹理走向，保持光影方向、色调、饱和度、对比度和景深一致。\n"
        "硬性约束：画面中不得残留任何人体部位（头、脸、手、手臂、腿、脚、头发）、"
        "衣物或人物轮廓边缘；不得出现模糊涂抹痕迹、模糊色块、克隆重复纹理、"
        "明显的修补边界或畸变。\n"
        "除人物外，其余内容必须与原图完全一致：不改动构图、不改变视角与焦距、"
        "不调整色调风格、不新增或删除任何景物、不添加文字水印。\n"
        "如果原图本身不含任何人物，则直接原样输出该图，不做任何修改。"
    )
    errors = []
    for model in settings.scene_extract_models:
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
                b64 = await asyncio.to_thread(download_image, b64, model)
            return b64
        except Exception as e:
            errors.append(f"{model}: {str(e)[:100]}")
    raise RuntimeError("；".join(errors))


async def _do_scene_auto_tag(data_uris: list[str]) -> tuple[dict[str, list[str]], str]:
    """③ 场景打标(五维枚举白名单防幻觉)+ 一句话描述。"""
    dims_json = json_mod.dumps(SCENE_DIMENSION_GROUPS, ensure_ascii=False, indent=2)
    system = (
        "你是电商场景标注专家。根据提供的场景图,从给定维度枚举中选择最合适的值,"
        "以 JSON 返回。规则:\n1. 每维可多选,也可以单选。\n"
        "2. 无法判断的维度返回空数组 []。\n"
        "3. 只使用枚举中出现的值,不要自己创造新值。\n"
        "4. 只输出 JSON,不带 markdown 或其他文字。"
    )
    user = (
        f"以下是维度枚举(JSON):\n{dims_json}\n\n"
        "请为这张场景图打标,严格按以下 JSON 格式返回:\n"
        '{"dims": {"space": [...], "region": [...], "season": [...], '
        '"weather": [...], "sceneStyle": [...]}, '
        '"description": "一句话描述这个场景"}'
    )
    pool = get_model_pool()
    resp, _used_model = await pool.chat_with_images(
        system=system, user=user, image_uris=data_uris,
        response_format={"type": "json_object"},
        reasoning_effort="close",
    )
    content = (resp.content or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(l for l in lines if not l.startswith("```"))
    data = json_mod.loads(content)

    raw_dims = data.get("dims", {}) if isinstance(data, dict) else {}
    validated: dict[str, list[str]] = {}
    for key, allowed in SCENE_DIMENSION_GROUPS.items():
        vals = raw_dims.get(key, [])
        if isinstance(vals, list):
            cleaned = [str(v) for v in vals if str(v) in allowed]
            if cleaned:
                validated[key] = cleaned
    desc = str(data.get("description", "")).strip() if isinstance(data, dict) else ""
    return validated, desc


async def _run_scene_task(task: dict, raw: bytes) -> None:
    """后台场景处理(协程,并发闸内):① 违规审核 ② 场景提取 ③ 打标+描述。

    顺序钉死:DB 行 UPDATE 成功后才写任务文件 done;违规/失败 → 行 failed。
    """
    sid = task.get("session_id") or "scene"
    scene_id = task.get("scene_id")

    async def fail(err: str) -> None:
        task["status"] = "failed"
        task["error"] = err
        await asyncio.to_thread(_task_store().save, task)
        if scene_id:
            await _amark_scene_status(int(scene_id), "failed")

    async with _PIPELINE_ASYNC_SEMAPHORE:
        try:
            # ── ① 违规审核(审原图) ──
            task["step"] = "checking"
            await asyncio.to_thread(_task_store().save, task)
            try:
                violation, reason = await _check_violation(raw)
            except Exception as e:
                await fail(f"违规审核失败: {str(e)[:400]}")
                return
            if violation:
                await fail(f"内容违规,已拦截: {reason[:200]}")
                return

            # ── ② 场景提取 ──
            task["step"] = "extracting"
            await asyncio.to_thread(_task_store().save, task)
            try:
                b64 = await _extract_scene_image(raw)
                cover_uri = await asyncio.to_thread(
                    _save_output, sid, f"scene-{int(time.time() * 1000)}", b64)
            except Exception as e:
                await fail(f"场景提取失败: {str(e)[:400]}")
                return
            task["cover_storage_uri"] = cover_uri

            # ── ③ 打标+描述(失败降级:dims 留空,用户手打) ──
            task["step"] = "tagging"
            await asyncio.to_thread(_task_store().save, task)
            dims: dict[str, list[str]] = {}
            desc = ""
            try:
                dims, desc = await _do_scene_auto_tag(
                    await asyncio.to_thread(_to_data_uris, [cover_uri]))
            except Exception:
                pass  # 降级不阻断
            task["dims"] = dims

            # ── 先 UPDATE 行 → 再写 done ──
            task["step"] = "saving"
            await asyncio.to_thread(_task_store().save, task)
            tags = ",".join(dict.fromkeys(v for vals in dims.values() for v in vals))[:512]
            try:
                async with AsyncSessionLocal() as db:
                    o = await SceneRepo(db).aget(int(scene_id)) if scene_id else None
                    if o is None:
                        raise RuntimeError(f"场景 {scene_id} 已被删除")
                    o.cover_storage_uri = cover_uri
                    o.dims = dims
                    o.desc = desc
                    o.tags = tags
                    o.status = "pending_confirm"
                    await db.commit()
            except Exception as e:
                await fail(f"处理结果写入失败: {str(e)[:400]}")
                return

            task["status"] = "done"
            task["error"] = ""
            await asyncio.to_thread(_task_store().save, task)
        except Exception as e:
            await fail(f"处理异常: {str(e)[:400]}")


async def _spawn_scene_task(o, session_id: str | None = None) -> str:
    """读原图 + 建任务文件 + 起后台协程,返回 task_id。"""
    raw = await run_in_threadpool(_read_original_image, o.original_storage_uri or "")
    sid = session_id or f"scene_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "task_type": "scene", "pid": _PID,
        "status": "processing", "step": "queued",
        "session_id": sid, "original_uri": o.original_storage_uri,
        "scene_id": o.id, "error": "",
    }
    await asyncio.to_thread(_task_store().save, task)
    _bg_tasks().spawn(_run_scene_task(task, raw))
    return task_id


# ---------------------------------------------------------------------------
# 响应转换
# ---------------------------------------------------------------------------

def _to_list_item(o) -> SceneListItem:
    return SceneListItem(
        id=o.id, scene_no=o.scene_no, name=o.name, desc=o.desc, tags=o.tags,
        scope=o.scope, origin=o.origin, status=o.status,
        cover_storage_uri=o.cover_storage_uri,
        cover_url=_storage_uri_url(o.cover_storage_uri),
        original_storage_uri=o.original_storage_uri,
        original_url=_storage_uri_url(o.original_storage_uri),
        created_at=to_cn_iso(o.created_at), updated_at=to_cn_iso(o.updated_at),
    )


def _to_detail(o) -> SceneDetailResponse:
    return SceneDetailResponse(
        id=o.id, scene_no=o.scene_no, name=o.name, desc=o.desc, tags=o.tags,
        scope=o.scope, origin=o.origin, status=o.status,
        cover_storage_uri=o.cover_storage_uri, cover_url=_storage_uri_url(o.cover_storage_uri),
        original_storage_uri=o.original_storage_uri,
        original_url=_storage_uri_url(o.original_storage_uri),
        dims=o.dims or {},
        created_at=to_cn_iso(o.created_at), updated_at=to_cn_iso(o.updated_at),
    )


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.get("/dimensions", summary="场景五维标签枚举")
async def get_scene_dimensions():
    return ok({"groups": SCENE_DIMENSION_GROUPS})


@router.get("/ai-status", summary="轮询场景处理任务状态")
async def scene_ai_status(task_id: str):
    await run_in_threadpool(_task_store().sweep)
    t = await asyncio.to_thread(_task_store().load, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return ok(t)


@router.get("", response_model=dict, summary="列出场景(scope/q/dims 筛选分页)")
async def list_scenes(
    scope: str | None = Query(default=None),
    q: str | None = Query(default=None),
    dims: str | None = Query(default=None, description='JSON:{"space":["客厅"]}'),
    page: int = Query(1),
    page_size: int = Query(20),
    db: AsyncSession = Depends(get_db_async),
):
    await run_in_threadpool(_task_store().sweep)
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

    repo = SceneRepo(db)
    items, total = await repo.alist(scope=scope, q=q, dims=parsed_dims, page=page, page_size=page_size)
    return ok(SceneListResponse(
        items=[_to_list_item(o) for o in items],
        total=total, page=page, page_size=page_size,
    ).model_dump())


@router.get("/{scene_id}", response_model=dict, summary="查询场景详情")
async def get_scene(scene_id: int, db: AsyncSession = Depends(get_db_async)):
    await run_in_threadpool(_task_store().sweep)
    repo = SceneRepo(db)
    o = await repo.aget(scene_id)
    if not o:
        raise HTTPException(404, "场景不存在")
    return ok(_to_detail(o).model_dump())


@router.post("/ai-extract", response_model=dict, summary="场景 AI 处理(点击即入库 extracting,后台审核+提取+打标)")
async def scene_ai_extract(body: SceneExtractRequest, db: AsyncSession = Depends(get_db_async)):
    """点击即入库:行 status=extracting,后台三步(违规审核→场景提取→打标)。

    scene_id 传了=对已有待确认行「换图重处理」(不新建)。
    完成 → pending_confirm(预填标签/描述);违规/失败 → failed。
    """
    raw = await run_in_threadpool(_read_original_image, body.original_uri)

    await run_in_threadpool(_task_store().sweep)

    sid = body.session_id or f"scene_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    repo = SceneRepo(db)

    if body.scene_id is not None:
        # 换图重处理:更新原图、清产物、转 extracting,不新建
        o = await repo.aget(body.scene_id)
        if o is None:
            raise HTTPException(404, f"场景 {body.scene_id} 不存在")
        if o.scope == "official":
            raise HTTPException(403, "官方资产为平台精选,禁止编辑")
        if o.status != "pending_confirm":
            raise HTTPException(400, f"当前状态 {o.status} 不允许换图重处理")
        o.original_storage_uri = body.original_uri
        o.cover_storage_uri = None
        o.dims = {}
        o.desc = None
        o.tags = None
        o.status = "extracting"
        await db.commit()
    else:
        # 新建行(点击即入库);批量并发撞 scene_no 时回滚重试一次
        tmp_name = f"AI场景{datetime.now():%m%d-%H%M}"
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                o = await repo.acreate(
                    name=tmp_name,
                    scope="mine",
                    origin="ai",
                    original_storage_uri=body.original_uri,
                    dims={},
                )
                o.status = "extracting"  # DB 默认 extracting,显式覆盖保持一致
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
                last_err = IntegrityError("scene_no 冲突")
        if o is None:
            raise HTTPException(400, f"创建失败: {last_err}")

    task_id = await _spawn_scene_task(o, sid)
    return ok({
        "scene_id": o.id, "scene_no": o.scene_no,
        "task_id": task_id, "status": "extracting",
    })


@router.put("/{scene_id}", response_model=dict, summary="编辑/确认入库/重新整理/审核(官方资产 403)")
async def update_scene(scene_id: int, body: SceneUpdateRequest, db: AsyncSession = Depends(get_db_async)):
    """四合一:
    - 确认入库:pending_confirm 行提交 {name, desc, tags, dims, status:"pending_review"}
    - 重新整理:pending_confirm 行提交 {status:"extracting"} → 后台重跑 AI 三步
    - 审核:pending_review 行提交 {status:"approved"/"returned"}
    - 普通编辑:不传 status
    """
    repo = SceneRepo(db)
    o = await repo.aget(scene_id)
    if not o:
        raise HTTPException(404, "场景不存在")
    if o.scope == "official":
        raise HTTPException(403, "官方资产为平台精选,禁止编辑")

    if body.status == "extracting":
        # 重新整理:清产物,后台重跑(失败保留旧结果 → 行会回到 pending_confirm 或 failed)
        if o.status != "pending_confirm":
            raise HTTPException(400, f"当前状态 {o.status} 不允许重新整理")
        o.cover_storage_uri = None
        o.dims = {}
        o.desc = None
        o.tags = None
        o.status = "extracting"
        await db.commit()
        task_id = await _spawn_scene_task(o)
        return ok({"scene_id": o.id, "scene_no": o.scene_no,
                   "task_id": task_id, "status": "extracting"})

    if body.status == "pending_review":
        if o.status != "pending_confirm":
            raise HTTPException(400, f"当前状态 {o.status} 不允许确认入库")
    elif body.status in ("approved", "returned"):
        if o.status != "pending_review":
            raise HTTPException(400, f"当前状态 {o.status} 不允许审核操作")

    try:
        o = await repo.aupdate(
            scene_id,
            name=body.name,
            desc=body.desc,
            tags=body.tags,
            cover_storage_uri=body.cover_storage_uri,
            original_storage_uri=body.original_storage_uri,
            dims=body.dims.model_dump() if body.dims is not None else None,
            status=body.status,
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ok(_to_detail(o).model_dump())


@router.delete("/{scene_id}", response_model=dict, summary="删除场景(官方资产 403)")
async def delete_scene(scene_id: int, db: AsyncSession = Depends(get_db_async)):
    repo = SceneRepo(db)
    o = await repo.aget(scene_id)
    if not o:
        raise HTTPException(404, "场景不存在")
    if o.scope == "official":
        raise HTTPException(403, "官方资产为平台精选,禁止删除")
    if not await repo.adelete(scene_id):
        raise HTTPException(404, "场景不存在")
    await db.commit()
    return ok({"deleted": True, "scene_id": scene_id})
