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

# 真实拆解配置
OUTFIT_VLM_MODEL = os.environ.get("OUTFIT_VLM_MODEL", "gemini-3.7-flash")  # VLM 单品识别
# 抠图降级链(gpt-image-2 优先,质量优先;mai 保底,人物图不被安全策略拦截)
OUTFIT_EXTRACT_MODELS = ["gpt-image-2", "gpt-image-2.5-flare", "mai-image-2.5"]
OUTFIT_MAX_ITEMS = 6                 # 单品数量上限(防 VLM 失控)
OUTFIT_EXTRACT_CONCURRENCY = 3       # 抠图并发数
OUTFIT_VLM_TIMEOUT = 120             # VLM 识别超时(秒)
_TASK_LOCK = threading.RLock()       # 任务文件写锁(可重入,worker 锁内调用 _save_task 不会自锁)

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
    with _TASK_LOCK:
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


# ---------------------------------------------------------------------------
# 真实拆解(二期已上线):VLM 识别 + 逐件白底抠图
# ---------------------------------------------------------------------------

def _extract_json(text: str):
    """从 VLM 返回文本提取 JSON(直接 parse → 剥 fence → 截 {..})。"""
    if not text:
        return {}
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    import re as _re
    fenced = _re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=_re.IGNORECASE)
    fenced = _re.sub(r"\s*```$", "", fenced)
    try:
        obj = json.loads(fenced.strip())
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    first = fenced.find("{")
    last = fenced.rfind("}")
    if first >= 0 and last > first:
        try:
            obj = json.loads(fenced[first:last + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return {}


def _call_vlm_recognize(raw: bytes):
    """VLM(gemini-3.7-flash)识别穿搭照片中的单品清单。

    Returns:
        [{"name","category","color"}, ...](≤ OUTFIT_MAX_ITEMS),失败抛 RuntimeError。
    """
    b64 = base64.b64encode(raw).decode()
    prompt = (
        "这是一张人物全身穿搭照片。请识别照片中人物身上穿/戴的每一件单品"
        "(上衣、裤装、鞋、包、腰带、眼镜等),输出 JSON:\n"
        '{"items":[{"name":"单品名(简洁,如 军绿衬衫外套)",'
        '"category":"品类(衬衫/T恤/裤子/鞋/包/配饰 等通用词)",'
        '"color":"颜色(简洁,如 军绿/米白/黑)"}]}\n'
        "要求:\n"
        "1. 按从上到下、从外到内排列\n"
        "2. 不要包含人物本身特征(发型、肤色、身材)\n"
        "3. 只输出 JSON,不要任何解释"
    )
    payload = {
        "model": OUTFIT_VLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是专业的电商服饰单品识别专家。"},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        AI_BASE + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + AI_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=OUTFIT_VLM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json(content)
    items = parsed.get("items")
    if not isinstance(items, list) or not items:
        raise RuntimeError(f"VLM 未返回有效单品清单: {content[:200]}")

    norm = []
    for it in items[:OUTFIT_MAX_ITEMS]:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        norm.append({
            "name": str(it["name"]).strip(),
            "category": str(it.get("category") or "").strip(),
            "color": str(it.get("color") or "").strip(),
        })
    if not norm:
        raise RuntimeError(f"VLM 单品清单为空: {content[:200]}")
    print(f"[outfit-ai] 🔍 VLM 识别 {len(norm)} 件单品", flush=True)
    return norm


def _extract_item_image(raw: bytes, name: str):
    """单件抠图:按降级链依次尝试,成功返回 b64,全败抛 RuntimeError。"""
    prompt = (
        f"提取这张穿搭照片中的「{name}」,生成干净的专业白底商品图。"
        "要求:只保留这一件单品,主体完整(被遮挡部分合理补全),"
        "背景纯白,居中构图,无阴影、无文字"
    )
    errors = []
    for model in OUTFIT_EXTRACT_MODELS:
        try:
            r = _call_model(model, raw, prompt)
            b64 = (r.get("data") or [{}])[0].get("b64_json", "")
            if not b64:
                raise RuntimeError(f"{model}: AI 未返回图片结果")
            return b64
        except Exception as e:
            errors.append(f"{model}: {str(e)[:100]}")
    raise RuntimeError("；".join(errors))


def _run_real_extract(task: dict, raw: bytes):
    """后台线程:真实拆解 = VLM 识别 → 逐件抠图(并发)→ 进度落盘。

    终态:
      - 全部成功 → done,items 每项带图片
      - 部分失败 → done + failed_items(成功部分不受影响)
      - 全部失败 → failed + 失败汇总
    """
    try:
        # ① VLM 识别(失败则整个任务 failed)
        try:
            recognized = _call_vlm_recognize(raw)
        except Exception as e:
            task["status"] = "failed"
            task["error"] = f"单品识别失败: {str(e)[:400]}"
            _save_task(task)
            return

        # 识别结果先行落盘:前端轮询立即可见清单与进度
        norm_items = [
            {"id": f"r{i + 1}", "name": it["name"], "category": it["category"],
             "color": it["color"], "image": ""}
            for i, it in enumerate(recognized)
        ]
        task["items"] = norm_items
        task["progress"] = {"done": 0, "total": len(norm_items)}
        task["failed_items"] = []
        _save_task(task)

        # ② 逐件抠图(线程池并发 OUTFIT_EXTRACT_CONCURRENCY)
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

            # 图片落盘放锁外(磁盘 I/O 不占锁)
            image_url = ""
            if b64 is not None:
                fname = f"item_{task['task_id']}_{idx}.png"
                (OUTFIT_AI_DIR / fname).write_bytes(base64.b64decode(b64))
                image_url = f"/static/outfit_ai/{fname}"

            # 结果记录 + 进度计算 + 任务文件写入在同一把锁内,
            # 保证进度单调且终值正确(消除并发写竞态)
            with _TASK_LOCK:
                if b64 is not None:
                    results[idx] = b64
                    task["items"][idx]["image"] = image_url
                else:
                    errors[idx] = err
                    task["failed_items"].append({
                        "name": norm_items[idx]["name"], "error": err})
                done = len(results) + len(errors)
                task["progress"] = {"done": done, "total": len(norm_items)}
                _save_task(task)
            print(f"[outfit-ai] 🖼️ 抠图进度 {task['progress']['done']}/{len(norm_items)} "
                  f"({'✅' if b64 is not None else '❌'} {norm_items[idx]['name']})", flush=True)

        with ThreadPoolExecutor(
            max_workers=min(OUTFIT_EXTRACT_CONCURRENCY, len(norm_items))
        ) as ex:
            list(ex.map(worker, range(len(norm_items))))

        # ③ 终态
        ok_n = len(results)
        total = len(norm_items)
        if ok_n == 0:
            task["status"] = "failed"
            task["error"] = (
                f"全部 {total} 件单品抠图失败: "
                + "；".join(errors.values())[:400]
            )
        else:
            task["status"] = "done"
            task["error"] = (
                f"{len(errors)} 件单品抠图失败(详见 failed_items)" if errors else ""
            )
        _save_task(task)
        print(f"[outfit-ai] ✅ 真实拆解完成 {ok_n}/{total}", flush=True)
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)[:600]
        _save_task(task)


@router.post("/ai-extract")
async def ai_extract(
    image: UploadFile = File(default=None),
    mode: str = Form(default=""),
    source_url: str = Form(default=""),
):
    """上传照片 → 立即返回 task_id(异步任务 + 轮询)。

    mode 自动判定:传了照片(上传/直链)→ real 真实拆解;
    无图(内置示例照片)→ demo 演示模式;显式传 mode=demo|real 可覆盖。
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

    # mode 自动判定:传了照片 → real;示例照片 → demo;显式参数覆盖
    has_photo = (image is not None) or bool(source_url.strip())
    if mode not in ("demo", "real"):
        mode = "real" if has_photo else "demo"

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
        "failed_items": [],
        "progress": {"done": 0, "total": 0},
        "error": "",
    }
    _save_task(task)
    target = _run_real_extract if mode == "real" else _run_demo_extract
    threading.Thread(target=target, args=(task, raw), daemon=True).start()
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
