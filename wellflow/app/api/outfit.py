# -*- coding: utf-8 -*-
"""穿搭库 API(参照 mannequins.py 规范:四层结构 + 两步式流程 + StandardResponse)。

流程(仿模特库):
  交互端点(无状态,只落盘/调 LLM,不写库):
    0. 上传      POST /api/wellflow/image/uploads?session_id=xxx(同事统一接口)
    1. 拆解      POST /api/outfit/ai-extract(original_uri → 识别+抠图 → outputs/)
    2. 平铺      POST /api/outfit/flatlay(items → 4:3 平铺图 → outputs/)
    3. 打标      POST /api/outfit/auto-tag(平铺图 → 六组维度打标建议)
  入库端点:
    4. 入库      POST /api/outfit(一次事务写 outfit 表)
    + 列表 GET /api/outfit / 详情 GET /{id} / 编辑 PUT / 删除 DELETE
"""

from __future__ import annotations

import asyncio
import base64
import json as json_mod
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from wellflow.app.api.utils import ok
from wellflow.app.config import settings
from wellflow.app.database import get_db
from wellflow.app.llm.factory import get_llm_client
from wellflow.app.repositories.outfit_repo import OutfitRepo
from wellflow.app.schemas.outfit_schemas import (
    OutfitAutoTagRequest, OutfitAutoTagResponse,
    OutfitCreateRequest, OutfitUpdateRequest, OutfitExtractRequest,
    OutfitFlatlayRequest,
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

# 抠图降级链(gpt-image-2 优先;mai 保底,人物图不被安全策略拦截)
EXTRACT_MODELS = ["gpt-image-2", "gpt-image-2.5-flare", "mai-image-2.5"]
MAX_ITEMS = 6
EXTRACT_CONCURRENCY = 3
_TASK_LOCK = threading.RLock()

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


# ---------------------------------------------------------------------------
# CRUD(入库 + 列表/详情/编辑/删除)
# ---------------------------------------------------------------------------

def _to_list_item(o) -> OutfitListItem:
    return OutfitListItem(
        id=o.id, outfit_no=o.outfit_no, name=o.name, desc=o.desc, tags=o.tags,
        scope=o.scope, origin=o.origin,
        cover_storage_uri=o.cover_storage_uri,
        cover_url=_storage_uri_url(o.cover_storage_uri),
        created_at=o.created_at.isoformat(), updated_at=o.updated_at.isoformat(),
    )


def _to_detail(o) -> OutfitDetailResponse:
    return OutfitDetailResponse(
        id=o.id, outfit_no=o.outfit_no, name=o.name, desc=o.desc, tags=o.tags,
        scope=o.scope, origin=o.origin, status=o.status,
        cover_storage_uri=o.cover_storage_uri, cover_url=_storage_uri_url(o.cover_storage_uri),
        original_storage_uri=o.original_storage_uri,
        original_url=_storage_uri_url(o.original_storage_uri),
        items=o.items or [], dims=o.dims or {},
        created_at=o.created_at.isoformat(), updated_at=o.updated_at.isoformat(),
    )


@router.get("/dimensions", summary="穿搭六组维度枚举(颜色含分组)")
def get_dimensions():
    return ok({"groups": OUTFIT_DIMENSION_GROUPS})


@router.get("/ai-status", summary="轮询拆解任务状态")
def ai_status(task_id: str):
    t = _load_task(task_id)
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
def list_outfits(
    scope: str | None = Query(default=None),
    q: str | None = Query(default=None),
    dims: str | None = Query(default=None, description='JSON:{"outfitStyle":["极简"]}'),
    page: int = Query(1),
    page_size: int = Query(20),
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

    repo = OutfitRepo(db)
    items, total = repo.list(scope=scope, q=q, dims=parsed_dims, page=page, page_size=page_size)
    return ok(OutfitListResponse(
        items=[_to_list_item(o) for o in items],
        total=total, page=page, page_size=page_size,
    ).model_dump())


@router.get("/{outfit_id}", response_model=dict, summary="查询穿搭详情")
def get_outfit(outfit_id: int, db: Session = Depends(get_db)):
    repo = OutfitRepo(db)
    o = repo.get(outfit_id)
    if not o:
        raise HTTPException(404, "穿搭不存在")
    return ok(_to_detail(o).model_dump())


@router.post("", response_model=dict, summary="确认入库(一次事务写 outfit 表)")
def create_outfit(body: OutfitCreateRequest, db: Session = Depends(get_db)):
    """穿搭最终入库:交互端点产物全部通过 storage_uri 引用,此处一次事务落库。"""
    repo = OutfitRepo(db)
    try:
        o = repo.create(
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
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"创建失败: {e}")
    return ok(_to_detail(o).model_dump())


@router.put("/{outfit_id}", response_model=dict, summary="更新穿搭(官方资产 403)")
def update_outfit(outfit_id: int, body: OutfitUpdateRequest, db: Session = Depends(get_db)):
    repo = OutfitRepo(db)
    o = repo.get(outfit_id)
    if not o:
        raise HTTPException(404, "穿搭不存在")
    if o.scope == "official":
        raise HTTPException(403, "官方资产为平台精选,禁止编辑")
    try:
        o = repo.update(
            outfit_id,
            name=body.name,
            desc=body.desc,
            tags=body.tags,
            cover_storage_uri=body.cover_storage_uri,
            original_storage_uri=body.original_storage_uri,
            items=[it.model_dump() for it in body.items] if body.items is not None else None,
            dims=body.dims.model_dump() if body.dims is not None else None,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ok(_to_detail(o).model_dump())


@router.delete("/{outfit_id}", response_model=dict, summary="删除穿搭(官方资产 403)")
def delete_outfit(outfit_id: int, db: Session = Depends(get_db)):
    repo = OutfitRepo(db)
    o = repo.get(outfit_id)
    if not o:
        raise HTTPException(404, "穿搭不存在")
    if o.scope == "official":
        raise HTTPException(403, "官方资产为平台精选,禁止删除")
    if not repo.delete(outfit_id):
        raise HTTPException(404, "穿搭不存在")
    db.commit()
    return ok({"deleted": True, "outfit_id": outfit_id})


# ---------------------------------------------------------------------------
# 交互端点 1:AI 拆解(异步任务 + 轮询;真实识别 + 逐件抠图)
# ---------------------------------------------------------------------------

def _call_vlm_recognize(raw: bytes):
    """VLM(gemini-3.7-flash)识别照片单品清单,≤ MAX_ITEMS 件。"""
    import asyncio as _a

    async def _go():
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
        client = get_llm_client("vlm", model_override=settings.llm_model_node3)  # 识别 VLM(网关已部署 gemini-3.7-flash)
        resp = await client.chat_with_images(
            system="你是专业的电商服饰单品识别专家。",
            user=prompt,
            image_uris=[f"data:image/png;base64,{b64}"],
            response_format={"type": "json_object"},
        )
        return resp.content or ""

    content = _a.run(_go())
    parsed = _extract_json(content)
    items = parsed.get("items")
    if not isinstance(items, list) or not items:
        raise RuntimeError(f"VLM 未返回有效单品清单: {content[:200]}")
    norm = []
    for it in items[:MAX_ITEMS]:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        norm.append({
            "name": str(it["name"]).strip(),
            "category": str(it.get("category") or "").strip(),
            "color": str(it.get("color") or "").strip(),
        })
    if not norm:
        raise RuntimeError(f"VLM 单品清单为空: {content[:200]}")
    return norm


def _extract_item_image(raw: bytes, name: str):
    """单件抠图:降级链依次尝试,成功返回 b64,全败抛 RuntimeError。"""
    import asyncio as _a

    async def _go():
        prompt = (
            f"提取这张穿搭照片中的「{name}」,生成干净的专业白底商品图。"
            "要求:只保留这一件单品,主体完整(被遮挡部分合理补全),"
            "背景纯白,居中构图,无阴影、无文字"
        )
        errors = []
        for model in EXTRACT_MODELS:
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
                return b64 if not b64.startswith("http") else None
            except Exception as e:
                errors.append(f"{model}: {str(e)[:100]}")
        raise RuntimeError("；".join(errors))

    return _a.run(_go())


def _run_extract(task: dict, raw: bytes, mode: str):
    """后台任务:demo=预制示例;real=VLM 识别+并发抠图。产物落 uploads/{sid}/outputs/。"""
    sid = task.get("session_id") or "outfit"
    try:
        if mode == "real":
            try:
                recognized = _call_vlm_recognize(raw)
            except Exception as e:
                task["status"] = "failed"
                task["error"] = f"单品识别失败: {str(e)[:400]}"
                _save_task(task)
                return
            norm_items = [
                {"id": f"r{i + 1}", "name": it["name"], "category": it["category"],
                 "color": it["color"], "storage_uri": ""}
                for i, it in enumerate(recognized)
            ]
            task["items"] = norm_items
            task["progress"] = {"done": 0, "total": len(norm_items)}
            task["failed_items"] = []
            _save_task(task)

            from concurrent.futures import ThreadPoolExecutor
            results: dict = {}
            errors: dict = {}

            def worker(idx: int):
                b64 = None
                err = ""
                try:
                    b64 = _extract_item_image(raw, norm_items[idx]["name"])
                except Exception as e:
                    err = str(e)[:200]
                uri = ""
                if b64 is not None:
                    uri = _save_output(sid, f"item-{idx:02d}", b64)
                with _TASK_LOCK:
                    if b64 is not None:
                        results[idx] = b64
                        task["items"][idx]["storage_uri"] = uri
                    else:
                        errors[idx] = err
                        task["failed_items"].append({"name": norm_items[idx]["name"], "error": err})
                    task["progress"] = {"done": len(results) + len(errors), "total": len(norm_items)}
                    _save_task(task)

            with ThreadPoolExecutor(max_workers=min(EXTRACT_CONCURRENCY, len(norm_items))) as ex:
                list(ex.map(worker, range(len(norm_items))))

            ok_n = len(results)
            if ok_n == 0:
                task["status"] = "failed"
                task["error"] = f"全部 {len(norm_items)} 件单品抠图失败: " + "；".join(errors.values())[:400]
            else:
                task["status"] = "done"
                task["error"] = (f"{len(errors)} 件单品抠图失败(详见 failed_items)" if errors else "")
        else:
            # demo:预制 6 件示例(图已内置,直接返回)
            task["items"] = [
                {**it, "storage_uri": f"/static/assets/outfit-demo/{it['id']}.png"}
                for it in DEMO_ITEMS
            ]
            task["progress"] = {"done": len(DEMO_ITEMS), "total": len(DEMO_ITEMS)}
            task["failed_items"] = []
            task["status"] = "done"
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)[:600]
    _save_task(task)


@router.post("/ai-extract", response_model=dict, summary="AI 拆解(异步任务+轮询;demo/real)")
async def ai_extract(body: OutfitExtractRequest):
    """original_uri + session_id + mode(显式优先,默认 real)。"""

    # 读取原图字节(http 直链 / 本地 storage_uri)
    if body.original_uri.startswith(("http://", "https://")):
        import urllib.request as _ur
        try:
            with _ur.urlopen(body.original_uri, timeout=20) as resp:
                raw = resp.read()
        except Exception:
            raise HTTPException(400, "原图链接无法打开,请先走统一上传")
    else:
        p = _resolve_local_uri(body.original_uri)
        if not p or not p.exists():
            raise HTTPException(400, f"原图不存在: {body.original_uri}")
        raw = p.read_bytes()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(400, "图片过大(>15MB)")

    mode = body.mode or "real"  # 显式传 mode 优先;默认真实识别
    task_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id, "mode": mode, "status": "processing",
        "session_id": body.session_id or f"outfit_{task_id}",
        "original_uri": body.original_uri,
        "items": [], "failed_items": [], "progress": {"done": 0, "total": 0}, "error": "",
    }
    _save_task(task)
    threading.Thread(target=_run_extract, args=(task, raw, mode), daemon=True).start()
    return ok({"task_id": task_id, "status": "processing", "session_id": task["session_id"]})


@router.post("/flatlay", response_model=dict, summary="已选单品合成 4:3 平铺总图(输出落 outputs/)")
async def flatlay(body: OutfitFlatlayRequest):

    try:
        from PIL import Image
    except ImportError:
        raise HTTPException(500, "服务端缺少 Pillow,无法合成平铺总图")

    opened = []
    for uri in body.items:
        if uri.startswith(("http://", "https://")):
            raise HTTPException(400, f"单品图必须是本地 storage_uri: {uri}")
        src = _resolve_local_uri(uri)
        if not src or not src.exists():
            raise HTTPException(400, f"单品图不存在: {uri}")
        opened.append(Image.open(src))

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

    import io as _io
    buf = _io.BytesIO()
    canvas.save(buf, format="PNG")
    sid = body.session_id or f"outfit_{uuid.uuid4().hex[:8]}"
    uri = _save_output(sid, f"flatlay-{int(time.time() * 1000)}", base64.b64encode(buf.getvalue()).decode())
    return ok({"flatlay_storage_uri": uri, "flatlay_url": _storage_uri_url(uri), "session_id": sid})


# ---------------------------------------------------------------------------
# 交互端点 3:自动打标(仿 mannequins.auto_tag:VLM 读图 + 枚举校验防幻觉)
# ---------------------------------------------------------------------------

@router.post("/auto-tag", response_model=dict, summary="入库前自动打标(六组维度+描述)")
async def auto_tag(body: OutfitAutoTagRequest):

    data_uris = _to_data_uris([body.image_uri])
    if not data_uris:
        raise HTTPException(400, f"无法读取 image_uri: {req.image_uri}")

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
        f"{'用户补充意图:' + body.extra_context if body.extra_context else ''}\n\n"
        "请为这张穿搭图打标,严格按以下 JSON 格式返回:\n"
        '{"dims": {"outfitStyle": [...], "category": [...], "color": [...], '
        '"material": [...], "fit": [...], "func": [...]}, '
        '"description": "一句话描述这套穿搭的风格气质", '
        '"suggested_name": "可选的穿搭名称(中文;无法建议可为 null)"}'
    )

    try:
        client = get_llm_client("vlm", model_override=settings.llm_model_node3)
        resp = await client.chat_with_images(
            system=system, user=user_text, image_uris=data_uris,
            response_format={"type": "json_object"},
        )
        content = (resp.content or "").strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(l for l in lines if not l.startswith("```"))
        data = json_mod.loads(content)
    except json_mod.JSONDecodeError as e:
        raise HTTPException(502, f"VLM 返回格式错误: {e}")
    except Exception as e:
        raise HTTPException(502, f"自动打标失败: {e}")

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
    return ok(OutfitAutoTagResponse(
        dims=validated,
        description=description,
        suggested_name=suggested if isinstance(suggested, str) else None,
        model=settings.llm_model_node3,
    ).model_dump())
