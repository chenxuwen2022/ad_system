# -*- coding: utf-8 -*-
"""穿搭库 AI 能力(新增文件):演示模式 AI 拆解 + 平铺总图合成。

1. POST /api/outfit/ai-extract  上传照片 → 立即返回 task_id,后台线程拆解
   一期为演示模式:返回 6 件预制示例单品(军绿衬衫外套/米白圆领内搭/
   炭灰直筒长裤/白色低帮运动鞋/黑色银扣腰带/黑色方框墨镜)。
   单品图按需生成并缓存:磁盘已有 → 直接返回;缺失 → 依次尝试
   mai-image-2.5 → gpt-image-2.5-flare → gpt-image-2 的 /v1/images/edits
   白底提取,成功后写入缓存目录;全部失败 → status=failed(不降级)。
   与场景库 /api/scene/ai-extract 同一异步任务+轮询模式,
   二期真实 AI 拆解只需替换任务处理器。

2. GET /api/outfit/ai-status   轮询拆解结果

3. POST /api/outfit/flatlay    已选单品图 → PIL 合成 4:3 平铺总图
   (1448x1086 浅色底,按件数 1-6 排版)
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

router = APIRouter(prefix="/api/outfit", tags=["穿搭库"])

BASE_DIR = Path(__file__).resolve().parent.parent
# AI 运行时产物目录(gitignore,不入库)
OUTFIT_AI_DIR = BASE_DIR / "static" / "outfit_ai"
# 演示模式预制单品图目录(缓存,入库 git 后网关不可达也能跑)
DEMO_ITEM_DIR = BASE_DIR / "static" / "assets" / "outfit-demo"
# 演示示例照片
SAMPLE_PHOTO = BASE_DIR / "static" / "assets" / "outfit-demo" / "original.png"

# AI 网关(与 scene_ai.py 同源)
AI_BASE = os.environ.get("AI_BASE", "http://192.168.110.254/v1")
AI_KEY = os.environ.get(
    "AI_KEY", "sk-VOt0ZKEhTKeYybPQeONFwQEsuCmlI7Mgrv5tuiID3E7tKO9Y"
)
AI_MODELS = ["mai-image-2.5", "gpt-image-2.5-flare", "gpt-image-2"]  # 自动降级链

# 演示模式 6 件示例单品(与 PM demo 一致)
DEMO_ITEMS = [
    {"id": "jacket", "name": "军绿衬衫外套", "category": "衬衫", "color": "军绿"},
    {"id": "tee", "name": "米白圆领内搭", "category": "T恤", "color": "米白"},
    {"id": "trousers", "name": "炭灰直筒长裤", "category": "裤子", "color": "灰"},
    {"id": "shoes", "name": "白色低帮运动鞋", "category": "鞋", "color": "白"},
    {"id": "belt", "name": "黑色银扣腰带", "category": "配饰", "color": "黑"},
    {"id": "sunglasses", "name": "黑色方框墨镜", "category": "配饰", "color": "黑"},
]


def _ensure_dir():
    OUTFIT_AI_DIR.mkdir(parents=True, exist_ok=True)
    DEMO_ITEM_DIR.mkdir(parents=True, exist_ok=True)


def _task_path(task_id: str) -> Path:
    return OUTFIT_AI_DIR / f"{task_id}.json"


def _save_task(task: dict):
    _ensure_dir()
    _task_path(task["task_id"]).write_text(
        json.dumps(task, ensure_ascii=False), encoding="utf-8"
    )


def _load_task(task_id: str):
    p = _task_path(task_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _multipart(fields):
    boundary = uuid.uuid4().hex
    body = b""
    for name, value in fields:
        if value is None:
            continue
        if name == "image":
            body += (
                "--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"upload.png\"\r\n"
                "Content-Type: image/png\r\n\r\n" % boundary
            ).encode() + value + b"\r\n"
        else:
            body += (
                "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, name, value)
            ).encode()
    body += ("--%s--\r\n" % boundary).encode()
    return body, boundary


def _call_model(model: str, raw: bytes, prompt: str):
    """单模型白底提取调用,408/429/5xx 重试 1 次,超时 240s。"""
    body, boundary = _multipart([
        ("model", model),
        ("image", raw),
        ("prompt", prompt),
        ("size", "1024x1024"),
        ("quality", "medium"),
        ("output_format", "png"),
    ])
    req = urllib.request.Request(
        AI_BASE + "/images/edits",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + AI_KEY,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
    )
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", "ignore")[:200]
            last = f"{model}: HTTP {e.code} {body_txt}"
            if e.code in (408, 429, 500, 502, 503) and attempt == 0:
                time.sleep(4)
                continue
            break
        except Exception as e:
            last = f"{model}: {str(e)[:150]}"
            if attempt == 0:
                time.sleep(4)
                continue
            break
    raise RuntimeError(last or f"{model}: 调用失败")


def _generate_demo_item(item: dict, raw: bytes):
    """为一件演示单品生成白底图并缓存到 DEMO_ITEM_DIR,成功返回 True。"""
    dst = DEMO_ITEM_DIR / f"{item['id']}.png"
    if dst.exists():
        return True
    prompt = (
        f"提取这张穿搭照片中的「{item['name']}」,生成干净的专业白底商品图,"
        "主体完整保留(含被遮挡部分合理补全),背景纯白,居中构图"
    )
    for model in AI_MODELS:
        try:
            r = _call_model(model, raw, prompt)
            data = (r.get("data") or [{}])[0]
            b64 = data.get("b64_json", "")
            if not b64:
                raise RuntimeError(f"{model}: AI 未返回图片结果")
            dst.write_bytes(base64.b64decode(b64))
            return True
        except Exception:
            continue
    return False


def _run_demo_extract(task: dict, raw: bytes):
    """后台线程:为 6 件演示单品准备白底图(缓存优先,缺失则 AI 生成)。"""
    try:
        ok_ids = [it["id"] for it in DEMO_ITEMS if _generate_demo_item(it, raw)]
        failed = [it for it in DEMO_ITEMS if it["id"] not in ok_ids]
        if failed:
            names = "、".join(it["name"] for it in failed)
            task["status"] = "failed"
            task["error"] = (
                f"{len(failed)}/{len(DEMO_ITEMS)} 件演示单品图生成失败({names})。"
                "请检查 AI 网关(192.168.110.254)连通后重试。"
            )
        else:
            task["status"] = "done"
            task["items"] = [
                {**it, "image": f"/static/assets/outfit-demo/{it['id']}.png"}
                for it in DEMO_ITEMS
            ]
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)[:600]
    _save_task(task)


@router.post("/ai-extract")
async def ai_extract(
    image: UploadFile = File(default=None),
    mode: str = Form(default="demo"),
    source_url: str = Form(default=""),
):
    """上传照片 → 立即返回 task_id(演示模式异步任务)。

    mode=demo:对示例照片拆解 6 件预制单品;未上传图片时使用内置示例照片。
    """
    _ensure_dir()
    if image is not None:
        raw = await image.read()
        if not raw:
            return {"code": 1, "msg": "图片内容为空"}
        if len(raw) > 15 * 1024 * 1024:
            return {"code": 1, "msg": "图片过大(>15MB)"}
    elif source_url.strip():
        # 图片直链:仅支持可直接打开的图片链接(不解析商品页/社交平台页面)
        if not source_url.strip().startswith(("http://", "https://")):
            return {"code": 1, "msg": "仅支持 http/https 图片直链"}
        try:
            req = urllib.request.Request(source_url.strip(), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except Exception:
            return {"code": 1, "msg": "图片链接无法打开,请检查链接或改用上传"}
        if not raw or len(raw) > 15 * 1024 * 1024:
            return {"code": 1, "msg": "图片为空或过大(>15MB)"}
    elif SAMPLE_PHOTO.exists():
        raw = SAMPLE_PHOTO.read_bytes()
    else:
        return {"code": 1, "msg": "未上传图片且示例照片不存在"}

    ts = int(time.time() * 1000)
    task_id = f"{ts}_{uuid.uuid4().hex[:6]}"

    # 原图落盘(供详情页与创建入库引用)
    orig_name = f"orig_{task_id}.png"
    (OUTFIT_AI_DIR / orig_name).write_bytes(raw)

    task = {
        "task_id": task_id,
        "mode": mode,
        "status": "processing",
        "original_url": f"/static/outfit_ai/{orig_name}",
        "items": [],
        "error": "",
    }
    _save_task(task)
    threading.Thread(target=_run_demo_extract, args=(task, raw), daemon=True).start()
    return {
        "code": 0,
        "data": {
            "task_id": task_id,
            "original_url": task["original_url"],
            "status": "processing",
        },
    }


@router.get("/ai-status")
def ai_status(task_id: str):
    """轮询拆解任务状态:processing / done / failed。"""
    t = _load_task(task_id)
    if not t:
        return {"code": 1, "msg": "任务不存在"}
    return {"code": 0, "data": t}


# ---------------------------------------------------------------------------
# 平铺总图合成
# ---------------------------------------------------------------------------

# 画布与底色(demo 同款 1448x1086,4:3)
FLATLAY_W, FLATLAY_H = 1448, 1086
FLATLAY_BG = (247, 245, 242)


def _layout_grid(n: int) -> tuple[int, int]:
    """按件数决定排版行列:(cols, rows)。
    1=1x1, 2=2x1, 3=3x1, 4=2x2, 5=3x2(末行居中), 6=3x2。"""
    if n <= 1:
        return 1, 1
    if n <= 3:
        return n, 1
    if n == 4:
        return 2, 2
    return 3, 2


@router.post("/flatlay")
async def flatlay(items_json: str = Form(...)):
    """由已选单品图合成 4:3 平铺总图,返回图片 URL。

    items_json: [{"name":"军绿衬衫外套","image":"/static/assets/outfit-demo/jacket.png"}]
    单品图需为白底图;输出落 static/outfit_ai/flatlay_<ts>_<rand>.png。
    """
    try:
        items = json.loads(items_json or "[]")
    except json.JSONDecodeError:
        raise HTTPException(400, "items_json 必须是合法 JSON 数组")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "至少需要一件单品")
    if len(items) > 6:
        raise HTTPException(400, "单品数量超出上限(最多 6 件)")

    try:
        from PIL import Image
    except ImportError:
        raise HTTPException(500, "服务端缺少 Pillow,无法合成平铺总图")

    # 打开全部单品图(路径 URL → 绝对路径)
    opened = []
    for it in items:
        url = it.get("image") if isinstance(it, dict) else None
        if not url:
            raise HTTPException(400, "items 每项必须含 image")
        if url.startswith(("http://", "https://")):
            raise HTTPException(400, f"单品图必须是本地路径: {url}")
        src = BASE_DIR / url.lstrip("/")
        if not src.exists():
            raise HTTPException(400, f"单品图不存在: {url}")
        opened.append(Image.open(src))

    cols, rows = _layout_grid(len(opened))
    canvas = Image.new("RGB", (FLATLAY_W, FLATLAY_H), FLATLAY_BG)

    cell_w = FLATLAY_W // cols
    cell_h = FLATLAY_H // rows
    padding = int(min(cell_w, cell_h) * 0.09)  # 格内 9% 留白

    # 末行不足 cols 个时居中
    total = len(opened)
    full_rows = total // cols
    last_row_n = total % cols

    idx = 0
    for row in range(rows):
        count_in_row = cols if row < full_rows else last_row_n
        if count_in_row == 0:
            break
        start_col = (cols - count_in_row) // 2  # 居中
        for c in range(count_in_row):
            img = opened[idx].convert("RGBA")
            # 透明底 → 白底
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.alpha_composite(img)
            img = bg.convert("RGB")

            # 缩放适配单元格(保持比例)
            max_w = cell_w - padding * 2
            max_h = cell_h - padding * 2
            ratio = min(max_w / img.width, max_h / img.height)
            if ratio < 1:
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)

            x0 = (start_col + c) * cell_w + (cell_w - img.width) // 2
            y0 = row * cell_h + (cell_h - img.height) // 2
            canvas.paste(img, (x0, y0))
            idx += 1

    _ensure_dir()
    out_name = f"flatlay_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.png"
    out_path = OUTFIT_AI_DIR / out_name
    canvas.save(out_path, format="PNG")
    return {"code": 0, "data": {"flatlay_url": f"/static/outfit_ai/{out_name}"}}
