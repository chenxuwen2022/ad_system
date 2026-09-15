# -*- coding: utf-8 -*-
"""AI 场景提取接口（异步）：上传 → 立即返回任务ID，后台线程调 AI，前端轮询状态"""
import base64
import json
import re
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile

router = APIRouter()

AI_BASE = os.environ.get("AI_BASE", "http://192.168.110.254/v1")
AI_KEY = os.environ.get(
    "AI_KEY", "sk-VOt0ZKEhTKeYybPQeONFwQEsuCmlI7Mgrv5tuiID3E7tKO9Y"
)
AI_MODEL = os.environ.get("AI_MODEL", "gpt-image-2")
AI_MODELS = ["mai-image-2.5", "gpt-image-2.5-flare", "gpt-image-2"]  # 自动降级链（mai优先，规避Azure人物安全拦截）
AI_TAG_MODEL = os.environ.get("AI_TAG_MODEL", "gemini-3.7-flash")  # 标签识别（多模态理解模型）

# 5 个标签维度及可选值（与前端下拉框一致）
TAG_DIMS = {
    "空间类型": ["纯色棚拍", "摄影棚", "客厅", "卧室", "厨房", "卫生间", "办公室", "商场", "街道", "公园", "体育场", "健身房", "山林", "雪山", "沙滩", "营地", "岩壁"],
    "地域环境": ["城市", "城市街头", "郊外", "森林", "山地", "雪地", "湖泊", "海边", "沙漠"],
    "季节": ["春", "夏", "秋", "冬"],
    "天气": ["阴天", "雨天", "雪天", "雾", "日落"],
    "背景风格": ["极简", "高级", "自然", "温馨", "生活化", "户外", "专业", "科技", "潮流", "轻奢", "都市"],
}

BASE_DIR = Path(__file__).resolve().parent.parent
SCENE_AI_DIR = BASE_DIR / "static" / "scene_ai"


def _ensure_dir():
    SCENE_AI_DIR.mkdir(parents=True, exist_ok=True)


def _task_path(task_id: str) -> Path:
    return SCENE_AI_DIR / f"{task_id}.json"


def _save_task(task: dict):
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


def _parse_json_content(content):
    """解析模型返回的 JSON（兼容 ```json 代码块 / 纯 JSON / 前后有说明文字）"""
    if isinstance(content, list):
        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    s = str(content).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:
        i = s.find("{")
        j = s.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                pass
    return None


def _recognize_dims(raw: bytes) -> dict:
    """调用多模态模型识别图片包含的 5 维场景标签，只保留给定选项内的值"""
    try:
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
        opts = "\n".join(f"{k}：{','.join(v)}" for k, v in TAG_DIMS.items())
        text = ("分析这张图片，识别它包含的拍摄场景标签。\n" + opts
                + "\n每类可多选，图片中没有的不选；不确定的类别输出空数组。"
                  "只输出 JSON，格式：{\"空间类型\":[\"...\"],\"地域环境\":[\"...\"],\"季节\":[\"...\"],\"天气\":[\"...\"],\"背景风格\":[\"...\"]}，不要输出其他文字。")
        payload = {
            "model": AI_TAG_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": text},
            ]}],
            "temperature": 0.1,
        }
        req = urllib.request.Request(
            AI_BASE + "/chat/completions",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + AI_KEY, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        content = (r.get("choices") or [{}])[0].get("message", {}).get("content", "")
        d = _parse_json_content(content)
        if not isinstance(d, dict):
            return {}
        out = {}
        for k, vals in TAG_DIMS.items():
            got = d.get(k)
            if isinstance(got, str):
                got = [x.strip() for x in got.replace("，", ",").split(",") if x.strip()]
            if isinstance(got, list):
                picked = [str(x).strip() for x in got if str(x).strip() in vals]
                if picked:
                    out[k] = picked
        return out
    except Exception:
        return {}


def _call_model(model: str, raw: bytes, prompt: str):
    """调用单个模型，408/429/5xx 重试1次，超时240s"""
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


def _run_ai(task: dict, raw: bytes, prompt: str):
    """后台线程：先识别图片标签，再按降级链生成场景图，更新任务状态"""
    # 1) 标签识别（多模态理解模型，先完成先返回）
    try:
        dims = _recognize_dims(raw)
        if dims:
            task["dims"] = dims
            _save_task(task)
    except Exception as e:
        task["tag_error"] = str(e)[:200]
        _save_task(task)
    # 2) 场景图生成（降级链）
    errors = []
    for model in AI_MODELS:
        try:
            r = _call_model(model, raw, prompt)
            data = (r.get("data") or [{}])[0]
            b64 = data.get("b64_json", "")
            if not b64:
                raise RuntimeError(f"{model}: AI 未返回图片结果")
            scene_bytes = base64.b64decode(b64)
            scene_name = f"scene_{task['task_id']}.png"
            (SCENE_AI_DIR / scene_name).write_bytes(scene_bytes)
            task["scene_url"] = f"/static/scene_ai/{scene_name}"
            task["status"] = "done"
            task["usage"] = r.get("usage", {})
            task["model"] = model
            _save_task(task)
            return
        except Exception as e:
            errors.append(str(e)[:200])
    task["status"] = "failed"
    task["error"] = "；".join(errors)[:600]
    _save_task(task)


@router.post("/ai-extract")
async def ai_extract_async(
    image: UploadFile = File(...),
    prompt: str = Form("提取这张图片中的场景部分，生成场景图片"),
):
    """上传图片 → 立即返回任务ID（AI 后台处理）"""
    raw = await image.read()
    if not raw:
        return {"code": 1, "msg": "图片内容为空"}
    if len(raw) > 15 * 1024 * 1024:
        return {"code": 1, "msg": "图片过大（>15MB）"}

    _ensure_dir()
    ts = int(time.time() * 1000)
    rand = uuid.uuid4().hex[:6]
    task_id = f"{ts}_{rand}"
    orig_name = f"orig_{task_id}.png"
    (SCENE_AI_DIR / orig_name).write_bytes(raw)

    task = {
        "task_id": task_id,
        "status": "processing",
        "orig_url": f"/static/scene_ai/{orig_name}",
        "scene_url": "",
        "usage": {},
        "error": "",
    }
    _save_task(task)
    threading.Thread(target=_run_ai, args=(task, raw, prompt), daemon=True).start()
    return {
        "code": 0,
        "data": {
            "task_id": task_id,
            "orig_url": task["orig_url"],
            "status": "processing",
        },
    }


@router.get("/ai-status")
def ai_status(task_id: str):
    """查询 AI 任务状态：processing / done / failed"""
    t = _load_task(task_id)
    if not t:
        return {"code": 1, "msg": "任务不存在"}
    return {"code": 0, "data": t}
