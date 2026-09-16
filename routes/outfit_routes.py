# -*- coding: utf-8 -*-
"""穿搭库 API(新增文件,不修改任何现有路由文件)。

读操作复用 /api/assets 列表(asset_routes.py 的列表直接透传记录全字段,
outfit 多图字段自动跟随),本文件补齐:
  1. GET  /api/assets/{asset_id}  单条详情(同前缀新路由,不冲突)
  2. GET  /api/outfit/dimensions  维度枚举(含颜色分组)
  3. POST /api/outfit             创建穿搭(多图 multipart + items_json)
  4. PUT  /api/outfit/{id}        编辑穿搭(官方资产 403)
  5. DELETE /api/outfit/{id}      删除穿搭(官方资产 403,删记录+关联图片文件)

数据仍存 assets_data/library.json(与场景库同库不同分类),
穿搭写操作的「读-改-写」整体持 _LOCK,防并发写丢数据。
"""

import json
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

# 与 asset_routes.py 同前缀的详情路由(新 router,不修改旧文件)
assets_router = APIRouter(prefix="/api/assets", tags=["资产库"])
# 穿搭库专用写端点
outfit_router = APIRouter(prefix="/api/outfit", tags=["穿搭库"])

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "assets_data" / "library.json"
# 用户资产图片落盘目录(官方种子图也在此目录)
OUTFIT_IMG_DIR = BASE_DIR / "static" / "assets" / "outfits"
OUTFIT_IMG_DIR.mkdir(parents=True, exist_ok=True)

_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# 维度枚举(与 PM demo 一致:品类/穿搭风格/颜色(分组)/材质/版型/功能属性)
# ---------------------------------------------------------------------------

COLOR_BASIC = ["黑", "白", "灰", "米", "米白", "棕", "卡其", "藏青", "军绿", "大地色"]
COLOR_COLORFUL = ["红", "橙", "黄", "绿", "蓝", "紫", "粉"]

DIMENSION_GROUPS = {
    "category": {
        "label": "品类",
        "options": ["羽绒服", "冲锋衣", "抓绒", "软壳", "T恤", "卫衣", "衬衫",
                    "裤子", "裙子", "内衣", "婴儿服", "家居服", "鞋", "包", "配饰"],
    },
    "outfitStyle": {
        "label": "穿搭风格",
        "options": ["极简", "通勤", "休闲", "运动", "户外", "机能",
                    "潮流", "亲子", "可爱", "高级"],
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

# 颜色可选值全集(筛选/校验用)
ALL_COLORS = COLOR_BASIC + COLOR_COLORFUL


# ---------------------------------------------------------------------------
# library.json 读写(记录结构与 asset_routes.py 一致,此处独立实现)
# ---------------------------------------------------------------------------

def _load() -> list:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list):
    DATA_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_outfit_id(items: list) -> str:
    """分配 WF-Oxxx 编号:取现有 outfit 记录最大数字 + 1。"""
    max_n = 0
    for it in items:
        if it.get("category") == "outfit":
            m = re.search(r"(\d+)$", it.get("id", ""))
            if m:
                max_n = max(max_n, int(m.group(1)))
    return f"WF-O{max_n + 1:03d}"


def _local_path(url: str) -> Path | None:
    """/static/... 或 uploads/... 相对 URL → 项目内绝对路径;http(s) 返回 None。"""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return None
    return BASE_DIR / url.lstrip("/")


def _copy_to_outfit_dir(url: str, prefix: str = "user") -> str:
    """把本地图片 URL 引用的文件复制进 static/assets/outfits/,返回新 URL。

    - http(s) URL:原样返回(不落盘)
    - 本地文件:复制为 user_<uuid>.<ext>;源文件不存在时抛 400
    """
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    src = _local_path(url)
    if not src or not src.exists():
        raise HTTPException(400, f"图片文件不存在: {url}")
    suffix = src.suffix.lower() or ".png"
    if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        suffix = ".png"
    dst_name = f"{prefix}_{uuid.uuid4().hex[:12]}{suffix}"
    shutil.copyfile(src, OUTFIT_IMG_DIR / dst_name)
    return f"/static/assets/outfits/{dst_name}"


def _remove_local_file(url: str) -> None:
    """删除本地图片文件(仅限 static/assets/outfits/ 下的 user_ 前缀文件,
    防止误删官方种子图)。失败仅告警不阻塞。"""
    if not url or url.startswith(("http://", "https://")):
        return
    p = _local_path(url)
    if not p or not p.exists():
        return
    if p.parent != OUTFIT_IMG_DIR or not p.name.startswith("user_"):
        return
    try:
        p.unlink()
    except Exception:
        pass


def _build_tags(dims: dict) -> str:
    """tags = 穿搭风格 + 品类 去重拼接(搜索/卡片文案用)。"""
    vals = []
    for key in ("outfitStyle", "category"):
        for v in dims.get(key) or []:
            if v and v not in vals:
                vals.append(v)
    return ",".join(vals)


def _build_desc(name: str, items: list, dims: dict) -> str:
    """描述自动概括:单品名、件数与穿搭风格。"""
    if not items:
        return name
    names = "、".join(it.get("name", "") for it in items if it.get("name"))
    styles = "、".join(dims.get("outfitStyle") or [])
    base = f"{names}组成的{len(items)}件搭配"
    if styles:
        base += f"，呈现{styles}风格"
    return base + "。"


def _get_outfit(items: list, outfit_id: str) -> dict | None:
    return next((it for it in items if it.get("category") == "outfit" and it.get("id") == outfit_id), None)


def _guard_editable(target: dict) -> None:
    if target.get("scope") == "official":
        raise HTTPException(403, "官方资产为平台精选,禁止编辑或删除")


# ---------------------------------------------------------------------------
# 读端点
# ---------------------------------------------------------------------------

@assets_router.get("/{asset_id}")
def get_asset_detail(asset_id: str):
    """查询单条资产详情(穿搭库详情页/编辑回填;对全部分类通用)。"""
    items = _load()
    target = next((it for it in items if it.get("id") == asset_id), None)
    if not target:
        raise HTTPException(404, f"资产不存在: {asset_id}")
    return {"code": 0, "data": target}


@outfit_router.get("/dimensions")
def get_outfit_dimensions():
    """穿搭库六组维度枚举,颜色维度含「基础色/彩色」分组。"""
    return {"code": 0, "data": {"groups": DIMENSION_GROUPS}}


# ---------------------------------------------------------------------------
# 写端点
# ---------------------------------------------------------------------------

@outfit_router.post("")
async def create_outfit(
    name: str = Form(...),
    desc: str = Form(default=""),
    dims: str = Form(default="{}"),
    tags: str = Form(default=""),
    scope: str = Form(default="mine"),
    origin: str = Form(default="upload"),
    original_url: str = Form(default=""),
    cover_url: str = Form(default=""),
    items_json: str = Form(default="[]"),
):
    """创建穿搭:original_url(原图)/cover_url(平铺总图)/items_json(单品数组)
    引用的是已落盘的图片 URL(ai-extract / flatlay 端点产物或用户上传)。

    items_json: [{"id":"...","name":"军绿衬衫外套","category":"衬衫",
                  "color":"军绿","image":"/static/..."}]
    """
    if origin not in ("upload", "url", "demo", "ai"):
        origin = "upload"
    if not name or not name.strip():
        raise HTTPException(400, "name 为必传参数")

    try:
        items_raw = json.loads(items_json or "[]")
    except json.JSONDecodeError:
        raise HTTPException(400, "items_json 必须是合法 JSON 数组")
    if not isinstance(items_raw, list) or not items_raw:
        raise HTTPException(400, "至少需要一件单品(items_json 非空数组)")
    try:
        dims_obj = json.loads(dims or "{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "dims 必须是合法 JSON 对象")
    if not isinstance(dims_obj, dict):
        dims_obj = {}

    # 图片落盘到资产目录,记录内只存最终 URL
    original_saved = _copy_to_outfit_dir(original_url, "user_orig")
    cover_saved = _copy_to_outfit_dir(cover_url, "user_cover")
    norm_items = []
    for it in items_raw:
        if not isinstance(it, dict) or not it.get("name"):
            raise HTTPException(400, "items 每项必须含 name")
        norm_items.append({
            "id": str(it.get("id") or uuid.uuid4().hex[:8]),
            "name": str(it["name"]).strip(),
            "category": str(it.get("category") or "").strip(),
            "color": str(it.get("color") or "").strip(),
            "image": _copy_to_outfit_dir(it.get("image") or "", "user_item"),
        })

    tags_final = tags.strip() or _build_tags(dims_obj)
    desc_final = desc.strip() or _build_desc(name.strip(), norm_items, dims_obj)

    with _LOCK:
        items = _load()
        outfit_id = _next_outfit_id(items)
        record = {
            "id": outfit_id,
            "category": "outfit",
            "name": name.strip(),
            "desc": desc_final,
            "tags": tags_final,
            "scope": "mine" if scope != "official" else "mine",
            "origin": origin,
            "cover": cover_saved,
            "original": original_saved,
            "items": norm_items,
            "dims": dims_obj,
            "image": cover_saved or original_saved,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        items.append(record)
        _save(items)
    return {"code": 0, "data": record}


@outfit_router.put("/{outfit_id}")
async def update_outfit(
    outfit_id: str,
    name: str = Form(default=""),
    desc: str = Form(default=""),
    dims: str = Form(default=""),
    tags: str = Form(default=""),
    cover_url: str = Form(default=""),
):
    """编辑穿搭:名称/描述/维度/标签 + 可选更换平铺总图。官方资产 403。"""
    with _LOCK:
        items = _load()
        target = _get_outfit(items, outfit_id)
        if not target:
            raise HTTPException(404, f"穿搭不存在: {outfit_id}")
        _guard_editable(target)

        if name.strip():
            target["name"] = name.strip()
        if desc.strip():
            target["desc"] = desc.strip()
        if tags.strip():
            target["tags"] = tags.strip()
        if dims:
            try:
                dims_obj = json.loads(dims)
            except json.JSONDecodeError:
                raise HTTPException(400, "dims 必须是合法 JSON 对象")
            if isinstance(dims_obj, dict):
                target["dims"] = dims_obj
                if not tags.strip():
                    target["tags"] = _build_tags(dims_obj)
        if cover_url:
            old_cover = target.get("cover")
            target["cover"] = _copy_to_outfit_dir(cover_url, "user_cover")
            target["image"] = target["cover"]
            if old_cover and old_cover != target["cover"]:
                _remove_local_file(old_cover)
        _save(items)
    return {"code": 0, "data": target}




@outfit_router.post("/upload")
async def upload_outfit_image(file: UploadFile = File(...)):
    """通用图片上传(编辑换图/创建时手动传图用):落盘并返回 URL。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "图片内容为空")
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(400, "图片过大(>15MB)")
    suffix = Path(file.filename or "img.png").suffix.lower() or ".png"
    if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        suffix = ".png"
    name = f"user_upload_{uuid.uuid4().hex[:12]}{suffix}"
    (OUTFIT_IMG_DIR / name).write_bytes(raw)
    return {"code": 0, "data": {"url": f"/static/assets/outfits/{name}"}}

@outfit_router.delete("/{outfit_id}")
def delete_outfit(outfit_id: str):
    """删除穿搭:记录 + 全部关联图片文件(原图/单品图/平铺图)。官方资产 403。"""
    with _LOCK:
        items = _load()
        target = _get_outfit(items, outfit_id)
        if not target:
            raise HTTPException(404, f"穿搭不存在: {outfit_id}")
        _guard_editable(target)

        # 先收集全部引用文件,再删记录与文件
        refs = [target.get("cover"), target.get("original"), target.get("image")]
        for it in target.get("items") or []:
            refs.append(it.get("image"))
        items = [it for it in items if not (it.get("category") == "outfit" and it.get("id") == outfit_id)]
        _save(items)
    for url in refs:
        _remove_local_file(url)
    return {"code": 0, "data": {"deleted": outfit_id}}
