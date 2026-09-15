# -*- coding: utf-8 -*-
"""资产库 CRUD 接口：品牌资产库/参考素材/模特库/穿搭库/场景库
数据保存在本地 JSON 文件（assets_data/library.json），图片上传到 static/assets/。
"""
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/assets", tags=["资产库"])

BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根目录
DATA_DIR = BASE_DIR / "assets_data"
DATA_FILE = DATA_DIR / "library.json"
IMG_DIR = BASE_DIR / "static" / "assets"
IMG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 分类定义：key → (中文名, 维度定义)
CATEGORIES = {
    "brand": "品牌资产库",
    "reference": "参考素材",
    "model": "模特库",
    "outfit": "穿搭库",
    "scene": "场景库",
}

DIM_DEFS = {
    # 维度key → 可选值列表
    "scene": {
        "空间类型": ["室内", "户外", "摄影棚", "半开放"],
        "地域环境": ["城市", "海边", "森林", "建筑", "乡村"],
        "季节": ["春", "夏", "秋", "冬", "不限"],
        "天气": ["晴", "阴", "雨", "雪", "黄昏", "夜景"],
        "背景风格": ["极简", "自然", "轻奢", "温馨", "高级", "专业", "生活化", "户外", "海边", "复古", "科技", "清新"],
    },
    "model": {
        "性别": ["女", "男", "儿童", "老人"],
        "风格": ["休闲", "职业", "时尚", "甜美", "运动"],
        "年龄": ["18-25", "25-35", "35-45", "45+"],
    },
    "outfit": {
        "风格": ["休闲", "职业", "时尚", "甜美", "运动", "复古"],
        "季节": ["春", "夏", "秋", "冬", "不限"],
        "适用场景": ["通勤", "约会", "旅行", "居家", "运动"],
    },
    "reference": {
        "类型": ["摄影", "插画", "海报", "视频", "文案"],
        "风格": ["极简", "自然", "复古", "科技", "国潮"],
    },
    "brand": {
        "行业": ["母婴", "美妆", "服饰", "食品", "家居", "数码"],
        "类型": ["品牌", "白牌", "工厂", "代理"],
    },
}


def _load() -> list:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list):
    DATA_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_id(category: str, items: list) -> str:
    prefix = {"brand": "WF-B", "reference": "WF-R", "model": "WF-M",
              "outfit": "WF-O", "scene": "WF-S"}[category]
    nums = []
    for it in items:
        if it.get("category") == category:
            m = re.search(r"(\d+)$", it.get("id", ""))
            if m:
                nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}{n:03d}"


@router.get("")
def list_assets(category: str = "", scope: str = "", q: str = "", dims: str = ""):
    """查询资产。dims 为 JSON 字符串：{"空间类型":"户外","季节":"夏"}"""
    items = _load()
    if category:
        items = [it for it in items if it.get("category") == category]
    if scope in ("official", "mine"):
        items = [it for it in items if it.get("scope") == scope]
    if q:
        # 分词：空格/逗号/顿号分隔。任一关键词命中即保留（OR），
        # 未命中的词自动尝试"去尾字"变体（如"夏天"→"夏"），支持描述式搜索
        tokens = [tk for tk in re.split(r"[ ,，、\s]+", q.lower()) if tk]
        if tokens:
            def _hit(it, tk):
                hay = (str(it.get("id", "")).lower()
                       + " " + str(it.get("name", "")).lower()
                       + " " + str(it.get("tags", "")).lower()
                       + " " + str(it.get("dims", {})).lower())
                if tk in hay:
                    return True
                if len(tk) > 1 and tk[:-1] in hay:
                    return True
                return False
            items = [it for it in items if any(_hit(it, tk) for tk in tokens)]
    if dims:
        try:
            fdims = json.loads(dims)
            norm = {}
            for k, v in fdims.items():
                if not v:
                    continue
                norm[k] = [str(x) for x in (v if isinstance(v, list) else [v])]
            if norm:
                items = [it for it in items if _dims_match(it.get("dims", {}), norm)]
        except Exception:
            pass
    items.sort(key=lambda x: x.get("id", ""))
    # 分类统计
    stats = {cat: sum(1 for it in _load() if it.get("category") == cat) for cat in CATEGORIES}
    return {"code": 0, "data": {"list": items, "total": len(items), "stats": stats}}


@router.post("")
async def create_asset(
    category: str = Form(...),
    name: str = Form(...),
    tags: str = Form(""),
    scope: str = Form("mine"),
    dims: str = Form("{}"),
    image: UploadFile | None = File(None),
    image_url: str = Form(""),
):
    if category not in CATEGORIES:
        raise HTTPException(400, f"未知分类: {category}")
    items = _load()
    asset_id = _next_id(category, items)
    img_url = (await _save_image(image)) if image else _valid_img_url(image_url)
    item = {
        "id": asset_id, "category": category, "name": name.strip(),
        "tags": tags.strip(), "scope": scope,
        "dims": _parse_dims(dims),
        "image": img_url,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    items.append(item)
    _save(items)
    return {"code": 0, "data": item}


@router.put("/{asset_id}")
async def update_asset(
    asset_id: str,
    name: str = Form(...),
    tags: str = Form(""),
    scope: str = Form("mine"),
    dims: str = Form("{}"),
    image: UploadFile | None = File(None),
    image_url: str = Form(""),
):
    items = _load()
    target = next((it for it in items if it.get("id") == asset_id), None)
    if not target:
        raise HTTPException(404, f"资产不存在: {asset_id}")
    if image:
        _remove_old_image(target)
        target["image"] = await _save_image(image)
    elif image_url:
        target["image"] = _valid_img_url(image_url)
    target["name"] = name.strip()
    target["tags"] = tags.strip()
    target["scope"] = scope
    target["dims"] = _parse_dims(dims)
    _save(items)
    return {"code": 0, "data": target}


@router.delete("/{asset_id}")
def delete_asset(asset_id: str):
    items = _load()
    target = next((it for it in items if it.get("id") == asset_id), None)
    if not target:
        raise HTTPException(404, f"资产不存在: {asset_id}")
    _remove_old_image(target)
    items = [it for it in items if it.get("id") != asset_id]
    _save(items)
    return {"code": 0, "data": {"deleted": asset_id}}


def _dims_match(ad: dict, fd: dict) -> bool:
    """多值维度匹配：查询值集合与资产值有任一交集即命中"""
    for k, vals in fd.items():
        av = ad.get(k)
        if isinstance(av, str):
            av = [av]
        if not av:
            return False
        avs = {str(x) for x in av}
        if not any(str(x) in avs for x in vals):
            return False
    return True


def _parse_dims(raw: str) -> dict:
    """维度值统一规范为 list（兼容单值 string）"""
    try:
        d = json.loads(raw) if raw else {}
        if not isinstance(d, dict):
            return {}
        out = {}
        for k, v in d.items():
            if isinstance(v, list):
                arr = [str(x).strip() for x in v if str(x).strip()]
            elif v not in ("", None):
                arr = [str(v).strip()]
            else:
                arr = []
            if arr:
                out[k] = arr
        return out
    except Exception:
        return {}


def _valid_img_url(url: str) -> str:
    u = url.strip()
    return u if u.startswith(("http://", "https://", "/")) else ""


async def _save_image(image: UploadFile) -> str:
    if not image.filename:
        return ""
    suffix = Path(image.filename).suffix.lower() or ".png"
    if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        suffix = ".png"
    fname = f"{uuid.uuid4().hex[:12]}{suffix}"
    dest = IMG_DIR / fname
    with dest.open("wb") as f:
        shutil.copyfileobj(image.file, f)
    return f"/static/assets/{fname}"


def _remove_old_image(item: dict):
    url = item.get("image", "")
    if url.startswith("/static/assets/"):
        p = BASE_DIR / url.lstrip("/")
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
