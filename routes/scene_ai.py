# -*- coding: utf-8 -*-
"""AI 场景提取接口（异步）：上传 → 立即返回任务ID，后台线程调 AI，前端轮询状态"""
import base64
import json
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


def _run_ai(task: dict, raw: bytes, prompt: str):
    """后台线程：调 gpt-image-2 生成场景图，更新任务状态"""
    try:
        body, boundary = _multipart([
            ("model", AI_MODEL),
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
        with urllib.request.urlopen(req, timeout=180) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        data = (r.get("data") or [{}])[0]
        b64 = data.get("b64_json", "")
        if not b64:
            raise RuntimeError("AI 未返回图片结果")
        scene_bytes = base64.b64decode(b64)
        scene_name = f"scene_{task['task_id']}.png"
        (SCENE_AI_DIR / scene_name).write_bytes(scene_bytes)
        task["scene_url"] = f"/static/scene_ai/{scene_name}"
        task["status"] = "done"
        task["usage"] = r.get("usage", {})
    except urllib.error.HTTPError as e:
        task["status"] = "failed"
        task["error"] = f"AI 服务错误 {e.code}: {e.read().decode('utf-8','ignore')[:300]}"
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)[:300]
    finally:
        _save_task(task)


@router.post("/ai-extract")
async def ai_extract_async(
    image: UploadFile = File(...),
    prompt: str = Form("提取这张图片中的主体物体并整理成干净、专业的电商商拍场景图，主体完整保留，背景干净统一，光线自然"),
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
