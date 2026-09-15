# -*- coding: utf-8 -*-
"""AI 场景提取接口：上传图片 → 调 gpt-image-2 处理成干净场景图，保留原图"""
import base64
import json
import os
import time
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


def _multipart(fields):
    """构造 multipart/form-data，fields 为 (name, value|None) 列表"""
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


@router.post("/ai-extract")
async def ai_extract(
    image: UploadFile = File(...),
    prompt: str = Form("提取这张图片中的主体物体并整理成干净、专业的电商商拍场景图，主体完整保留，背景干净统一，光线自然"),
):
    """上传图片 → AI 处理生成场景图，同时保留原图。返回两张图的 URL。"""
    raw = await image.read()
    if not raw:
        return {"code": 1, "msg": "图片内容为空"}
    if len(raw) > 15 * 1024 * 1024:
        return {"code": 1, "msg": "图片过大（>15MB）"}

    _ensure_dir()
    ts = int(time.time() * 1000)
    rand = uuid.uuid4().hex[:6]
    orig_name = f"orig_{ts}_{rand}.png"
    scene_name = f"scene_{ts}_{rand}.png"
    orig_path = SCENE_AI_DIR / orig_name
    scene_path = SCENE_AI_DIR / scene_name

    # 1) 保存原图
    orig_path.write_bytes(raw)

    # 2) 调 AI（gpt-image-2 /images/edits）
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
    usage = {}
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            r = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:400]
        return {"code": 1, "msg": f"AI 服务错误 {e.code}: {detail}"}
    except Exception as e:
        return {"code": 1, "msg": f"AI 服务连接失败: {e}"}

    data = (r.get("data") or [{}])[0]
    b64 = data.get("b64_json", "")
    if not b64:
        return {"code": 1, "msg": "AI 未返回图片结果"}
    try:
        scene_bytes = base64.b64decode(b64)
    except Exception:
        return {"code": 1, "msg": "AI 结果解码失败"}
    scene_path.write_bytes(scene_bytes)

    usage = r.get("usage", {})
    return {
        "code": 0,
        "data": {
            "scene_url": f"/static/scene_ai/{scene_name}",
            "orig_url": f"/static/scene_ai/{orig_name}",
            "model": r.get("model", AI_MODEL),
            "usage": usage,
        },
    }
