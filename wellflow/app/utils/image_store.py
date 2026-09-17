"""图片文件落盘 + 路径 ↔ data URI 转换工具。

目的：让 LangGraph state 只存文件路径（几 KB），不再塞 base64 data URI（几 MB），
从而让 AsyncPostgresSaver checkpoint 写入从「几 MB JSON」降到「几 KB JSON」，
消除 VLM 返回后 interrupt 推送延迟。

批量压缩策略（paths_to_data_uris）：
  - 最多取前 image_max_per_call 张（超出忽略）
  - 逐张判断：单张原始 ≤ 5MB → 不压缩，保持原编码原质量
  - 单张原始 > 5MB → PIL 压缩到 ≤ 5MB：quality 从 90 起步逐步降（最低 50），再超限才 resize 长边到 2800px

用法：
  from wellflow.app.utils.image_store import save_upload, path_to_data_uri, paths_to_data_uris

  # API 层收到 UploadFile → 落盘 → state 存路径
  paths = save_upload(task_id, upload_files)       # → ["uploads/taskX/p0.jpg", ...]

  # Node 入口：路径 → data URI（只在 LLM 调用瞬间生成，不进 state）
  data_uris = paths_to_data_uris(state_paths)      # → ["data:image/jpeg;base64,...", ...]

  # Interrupt payload 给前端展示时用
  data_uris = paths_to_data_uris(state_paths)
"""

from __future__ import annotations

import base64
import io
import mimetypes
import os
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# 压缩策略常量 —— 从 config.settings 统一读取（.env 可覆盖）
# ---------------------------------------------------------------------------
def _image_constants():
    """延迟读取，避免模块 import 时 settings 还没初始化。"""
    from wellflow.app.config import settings
    return {
        "MAX_IMAGES_PER_CALL": settings.image_max_per_call,
        "SINGLE_THRESHOLD_RAW_MB": settings.image_single_compress_threshold_mb,
    }

PIL_MAX_SIDE_FALLBACK = 2800         # 极端兜底：quality=50 仍超限时才收缩长边到这里
PIL_QUALITY_START = 90               # JPEG 质量起点
PIL_QUALITY_MIN = 50                 # JPEG 质量下限（低于此值画质不可接受）


def _get_upload_dir() -> Path:
    from wellflow.app.config import settings
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def save_upload(
    task_id: str,
    files: Sequence[tuple[str, bytes, str | None]],
    prefix: str = "p",
) -> list[str]:
    """把上传文件的原始字节落盘，返回相对路径列表。

    Args:
        task_id: 任务 ID，文件存到 uploads/{task_id}/ 下
        files: [(original_filename, raw_bytes, content_type), ...]
        prefix: 文件前缀，如 "p"（product）/ "m"（model）

    Returns:
        相对路径列表，如 ["uploads/taskX/p0.jpg", "uploads/taskX/p1.webp"]
    """
    base = _get_upload_dir()
    task_dir = base / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    paths: list[str] = []
    for i, (orig_name, raw, content_type) in enumerate(files):
        ext = mimetypes.guess_extension(content_type or "") if content_type else None
        if not ext or ext == ".jpe":
            ext = Path(orig_name).suffix or ".jpg"
        ext = ext.lstrip(".")

        from wellflow.app.config import settings
        ext = settings.image_ext_map.get(ext, ext)

        filename = f"{prefix}{i}.{ext}"
        save_path = task_dir / filename
        save_path.write_bytes(raw)

        rel_path = f"uploads/{task_id}/{filename}"
        paths.append(rel_path)
        print(f"[image_store] 💾 {rel_path} ({len(raw)} bytes, mime={content_type or '?'})", flush=True)

    return paths


def save_asset_upload(
    files: Sequence[tuple[str, bytes, str | None]],
    upload_id: str | None = None,
    *,
    session_id: str | None = None,
    filename_prefix: str = "i",
) -> tuple[str, list[dict]]:
    """把上传图片落盘，统一入口（通用素材库上传 + 模特 session 上传共用）。

    两种目录策略（互斥，优先 session_id）：
      - 传了 session_id → uploads/{session_id}/i0.jpg, i1.jpg ...（模特创建流程）
      - 没传 session_id → uploads/assets/{upload_id}/i0.jpg ...（通用素材库，upload_id 自动生成）

    Args:
        files: [(original_filename, raw_bytes, content_type), ...]
        upload_id: 没传 session_id 时用的目录 ID；不传则自动生成 UUID4 hex 前 8 位
        session_id: 模特创建流程的 session ID；传了就存 uploads/{session_id}/
        filename_prefix: 文件名前缀，默认 "i"（upload_id 模式）；模特模式传 "m" 也兼容

    Returns:
        (dir_id, images_info_list) —— dir_id 是实际使用的目录 ID（upload_id 或 session_id）
    """
    import uuid

    base = _get_upload_dir()

    if session_id:
        # 模式 A：绑定 session（模特创建流程）
        dir_id = session_id
        target_dir = base / dir_id
        storage_prefix = f"uploads/{dir_id}"
    else:
        # 模式 B：通用素材库
        dir_id = upload_id or uuid.uuid4().hex[:8]
        target_dir = base / "assets" / dir_id
        storage_prefix = f"uploads/assets/{dir_id}"

    target_dir.mkdir(parents=True, exist_ok=True)

    from wellflow.app.config import settings

    images: list[dict] = []
    for i, (orig_name, raw, content_type) in enumerate(files):
        ext = mimetypes.guess_extension(content_type or "") if content_type else None
        if not ext or ext == ".jpe":
            ext = Path(orig_name).suffix or ".jpg"
        ext = ext.lstrip(".")
        ext = settings.image_ext_map.get(ext, ext)

        filename = f"{filename_prefix}{i}.{ext}"
        save_path = target_dir / filename
        save_path.write_bytes(raw)

        rel_path = f"{storage_prefix}/{filename}"
        mime = content_type or mimetypes.guess_type(filename)[0] or "image/jpeg"
        size = len(raw)

        # 读取图片尺寸（Pillow 不可用时兜底 0x0）
        w, h = 0, 0
        try:
            from PIL import Image as _PIL_Image
            with _PIL_Image.open(io.BytesIO(raw)) as _im:
                w, h = _im.size
        except Exception:
            pass

        url = "/" + rel_path.lstrip("/")
        images.append({
            "index": i,
            "original_name": orig_name,
            "filename": filename,
            "storage_uri": rel_path,
            "url": url,
            "mime": mime,
            "size": size,
            "width": w,
            "height": h,
        })
        tag = "session" if session_id else "asset"
        print(f"[image_store] 💾 {tag} {rel_path} ({size}B, {mime}, {w}x{h})", flush=True)

    return dir_id, images


def _pil_compress(raw: bytes, quality: int, max_side: int | None = None) -> tuple[bytes, str, int, int]:
    """PIL 处理单张图片 → (jpeg_bytes, 'image/jpeg', new_w, new_h)。

    - max_side=None：不 resize，只做格式转换（PNG/WebP → JPEG）+ 指定 quality
    - max_side 有值：长边缩到 ≤ max_side（若原图已更小则不动）再 JPEG
    - 自动转 RGB（JPEG 不支持 alpha / palette）

    调用方需要自己 catch ImportError / 异常做 fallback。
    """
    from PIL import Image

    im = Image.open(io.BytesIO(raw))
    w, h = im.size
    new_w, new_h = w, h

    if max_side is not None and max(w, h) > max_side:
        ratio = max_side / max(w, h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        im = im.resize((new_w, new_h), Image.LANCZOS)

    if im.mode in ("RGBA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "image/jpeg", new_w, new_h


def _resolve_path(path: str) -> Path:
    """相对路径（如 uploads/taskX/p0.jpg）→ 基于项目根的绝对路径。

    项目根 = upload_dir.parent = wellflow/（本地）或 /app（容器内）。
    绝对路径原样返回。
    """
    abs_path = Path(path)
    if abs_path.is_absolute():
        return abs_path
    return _get_upload_dir().parent / path


def path_to_data_uri(path: str) -> str:
    """单张路径 → data URI，不做压缩（原始 base64）。

    文件不存在时返回空字符串。
    批量场景请用 paths_to_data_uris — 那里才做统一压缩决策。
    """
    abs_path = _resolve_path(path)
    if not abs_path.exists():
        print(f"[image_store] ⚠️ 路径不存在，跳过: {path}", flush=True)
        return ""

    raw = abs_path.read_bytes()
    mime, _ = mimetypes.guess_type(str(abs_path))
    mime = mime or "image/jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def paths_to_data_uris(paths: Sequence[str]) -> list[str]:
    """批量路径 → data URI，带逐张压缩决策。

    规则：
      - 已经是 data URI 的元素直接保留（老任务 checkpoint 兼容）
      - 最多取前 image_max_per_call 张（config 里配置）
      - 每张独立判断：原始 ≤ threshold → 原样 base64；> threshold → PIL 压缩到 ≤ threshold

    Pillow 不可用时安全降级（原样 base64，会恢复到原始 payload —
    若此时代理仍断连，由 ofox_gateway 重试 + 上层错误处理兜底）。
    """
    constants = _image_constants()
    MAX_IMAGES_PER_CALL = constants["MAX_IMAGES_PER_CALL"]
    SINGLE_THRESHOLD_MB = constants["SINGLE_THRESHOLD_RAW_MB"]
    threshold_bytes = int(SINGLE_THRESHOLD_MB * 1024 * 1024)

    # 1) 分离：data URI 原样保留，path 收集后逐张处理
    passthrough: list[str] = []
    path_items: list[tuple[str, bytes]] = []  # (path_str, raw_bytes)

    for p in paths:
        if not p:
            continue
        if p.startswith("data:"):
            passthrough.append(p)
            continue
        if len(path_items) >= MAX_IMAGES_PER_CALL:
            print(f"[image_store] 🪒 忽略第 {MAX_IMAGES_PER_CALL + 1} 张起的图片（上限 {MAX_IMAGES_PER_CALL}）", flush=True)
            break
        abs_path = _resolve_path(p)
        if not abs_path.exists():
            print(f"[image_store] ⚠️ 路径不存在，跳过: {p}", flush=True)
            continue
        path_items.append((p, abs_path.read_bytes()))

    result: list[str] = list(passthrough)
    if not path_items:
        return result

    # 2) 确认 Pillow 可用（只在有图需要压缩时才警告）
    pil_available = True
    try:
        __import__("PIL.Image")
    except ImportError:
        pil_available = False

    compressed_count = 0

    # 3) 逐张处理
    for p, raw in path_items:
        abs_path = _resolve_path(p)
        mime, _ = mimetypes.guess_type(str(abs_path))
        mime = mime or "image/jpeg"

        if len(raw) <= threshold_bytes:
            # 小图：原样 base64，不走 PIL
            b64 = base64.b64encode(raw).decode("ascii")
            result.append(f"data:{mime};base64,{b64}")
            continue

        # 大图 → 需要压缩
        if not pil_available:
            print(f"[image_store] ⚠️ 未安装 Pillow，无法压缩 {p} ({len(raw)/1024/1024:.1f}MB)，原样 base64", flush=True)
            b64 = base64.b64encode(raw).decode("ascii")
            result.append(f"data:{mime};base64,{b64}")
            continue

        enc, final_q, used_resize = _compress_single(raw, threshold_bytes)
        compressed_count += 1

        # 读取原图尺寸用于日志
        w_orig, h_orig = 0, 0
        try:
            from PIL import Image as _I
            _im = _I.open(io.BytesIO(raw))
            w_orig, h_orig = _im.size
        except Exception:
            pass

        # 读新尺寸
        try:
            from PIL import Image as _I
            _im2 = _I.open(io.BytesIO(enc))
            w_new, h_new = _im2.size
        except Exception:
            w_new, h_new = w_orig, h_orig

        resize_tag = " [resize]" if used_resize else ""
        print(
            f"[image_store] 🗜️ {p}: {len(raw)/1024/1024:.1f}MB → {len(enc)/1024:.0f}KB "
            f"({w_orig}x{h_orig} → {w_new}x{h_new}, q={final_q}){resize_tag}",
            flush=True,
        )
        b64 = base64.b64encode(enc).decode("ascii")
        result.append(f"data:image/jpeg;base64,{b64}")

    # 4) 汇总日志
    total_raw = sum(len(raw) for _, raw in path_items)
    print(
        f"[image_store] ✅ {len(path_items)} 张图片处理完成 "
        f"(原图合计 {total_raw/1024/1024:.1f}MB, 压缩 {compressed_count} 张, 阈值 {SINGLE_THRESHOLD_MB:.1f}MB/张)",
        flush=True,
    )
    return result


def _compress_single(raw: bytes, threshold_bytes: int) -> tuple[bytes, int, bool]:
    """压缩单张图片到 ≤ threshold_bytes。

    Returns:
        (compressed_bytes, final_quality, used_resize)
    """
    # 先尝试只降 quality（不 resize）
    best_enc = raw
    best_q = PIL_QUALITY_START
    for q in range(PIL_QUALITY_START, PIL_QUALITY_MIN - 1, -10):
        try:
            enc, _, _, _ = _pil_compress(raw, quality=q)  # max_side=None
        except Exception as e:
            print(f"[image_store] ⚠️ PIL 压缩失败 (q={q}, {e})", flush=True)
            break
        best_enc = enc
        best_q = q
        if len(enc) <= threshold_bytes:
            return enc, q, False
        print(f"[image_store] 🔍 q={q} 无resize: {len(enc)/1024:.0f}KB / target={threshold_bytes/1024:.0f}KB", flush=True)

    # quality 降到最低还超限 → 兜底 resize
    try:
        enc, _, _, _ = _pil_compress(raw, quality=best_q, max_side=PIL_MAX_SIDE_FALLBACK)
        print(f"[image_store] 🔍 兜底 side={PIL_MAX_SIDE_FALLBACK} q={best_q}: {len(enc)/1024:.0f}KB", flush=True)
        return enc, best_q, True
    except Exception as e:
        print(f"[image_store] ⚠️ PIL resize 失败 ({e})，退回 quality-only 结果", flush=True)
        return best_enc, best_q, False


def bytes_items_to_data_uris(
    items: list[tuple[bytes, str]],
) -> list[str]:
    """直接从 (raw_bytes, mime_type) 列表转 data URI，带逐张压缩决策。

    跳过文件落盘环节 —— 交互端点（optimize-prompt / generate / fine-tune / auto-tag）
    读 multipart 的文件字节后直接喂 LLM。

    规则同 paths_to_data_uris：最多取前 image_max_per_call 张，单张 ≤ threshold 原样，
    > threshold 走 PIL 压缩。

    Args:
        items: [(raw_bytes, mime_type), ...]

    Returns:
        data URI 列表
    """
    import base64 as _b64

    constants = _image_constants()
    MAX_IMAGES_PER_CALL = constants["MAX_IMAGES_PER_CALL"]
    SINGLE_THRESHOLD_MB = constants["SINGLE_THRESHOLD_RAW_MB"]
    threshold_bytes = int(SINGLE_THRESHOLD_MB * 1024 * 1024)

    result: list[str] = []
    pil_available = True
    try:
        __import__("PIL.Image")
    except ImportError:
        pil_available = False

    for raw, mime in items[:MAX_IMAGES_PER_CALL]:
        mime = mime or "image/jpeg"
        if len(raw) <= threshold_bytes:
            # 小图原样
            b64 = _b64.b64encode(raw).decode("ascii")
            result.append(f"data:{mime};base64,{b64}")
            continue

        if not pil_available:
            print(f"[image_store] ⚠️ 未安装 Pillow，无法压缩内存图片 ({len(raw)/1024/1024:.1f}MB)，原样 base64", flush=True)
            b64 = _b64.b64encode(raw).decode("ascii")
            result.append(f"data:{mime};base64,{b64}")
            continue

        enc, final_q, used_resize = _compress_single(raw, threshold_bytes)
        compressed_count = 1
        resize_tag = " [resize]" if used_resize else ""
        print(
            f"[image_store] 🗜️ 内存图片: {len(raw)/1024/1024:.1f}MB → {len(enc)/1024:.0f}KB "
            f"(q={final_q}){resize_tag}",
            flush=True,
        )
        b64 = _b64.b64encode(enc).decode("ascii")
        result.append(f"data:image/jpeg;base64,{b64}")

    return result


def is_path(value: str) -> bool:
    """判断一个字符串是文件路径（以 'uploads/' 开头或文件存在）还是 data URI。"""
    if value.startswith("data:"):
        return False
    if value.startswith("uploads/") or value.startswith("/"):
        return True
    try:
        return os.path.exists(value)
    except (OSError, ValueError):
        return False


def save_output_image(task_id: str, name: str, image_url: str) -> str:
    """把一张生成图（data URI）落盘到 uploads/{task_id}/outputs/{name}.{ext}，返回相对路径。

    Args:
        task_id: 任务 ID，文件存到 uploads/{task_id}/outputs/ 下
        name: 文件名主体（如 work_item_id "shot-01"），自动补扩展名
        image_url: data URI（data:image/png;base64,...），也兼容远程 http(s) url

    Returns:
        相对路径，如 "uploads/taskX/outputs/shot-01.png"；
        非 data URI（远程 url）时不落盘，原样返回该 url。
    """
    if not image_url:
        return ""
    if not image_url.startswith("data:"):
        # 远程 URL —— 不在本地落盘，直接存 url 作为 storage_uri
        return image_url

    mime, b64 = _parse_data_uri(image_url)
    ext = (mimetypes.guess_extension(mime or "") or ".png").lstrip(".") or "png"

    base = _get_upload_dir()
    out_dir = base / task_id / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{name}.{ext}"
    save_path = out_dir / filename
    save_path.write_bytes(base64.b64decode(b64))

    rel_path = f"uploads/{task_id}/outputs/{filename}"
    print(f"[image_store] 💾 成品图落盘 {rel_path} ({len(b64)//1024:.0f}KB b64)", flush=True)
    return rel_path


def _parse_data_uri(data_uri: str) -> tuple[str, str]:
    """data:image/{fmt};base64,<b64> → ("image/{fmt}", "<b64>")。"""
    import re
    m = re.match(r"data:([^;]+);base64,(.+)", data_uri, re.DOTALL)
    if not m:
        raise ValueError(f"无效 data URI: {data_uri[:80]}...")
    return m.group(1), m.group(2)


def delete_task_files(task_id: str) -> None:
    """安全删除 uploads/{task_id}/ 下的所有文件（含 outputs/ 子目录）。

    做了路径校验：确保解析后的绝对路径严格在 upload_dir 内部，
    防止 task_id 含 ../ 等路径穿越字符。目录不存在时静默跳过。
    """
    import shutil

    upload_dir = _get_upload_dir().resolve()
    task_dir = (upload_dir / task_id).resolve()

    # 安全校验：task_dir 必须严格位于 upload_dir 之下
    if upload_dir not in task_dir.parents and task_dir != upload_dir:
        print(f"[image_store] ⚠️ 路径穿越拒绝: task_dir={task_dir} upload_dir={upload_dir}", flush=True)
        return

    if not task_dir.exists():
        print(f"[image_store] ℹ️ 目录不存在，跳过删除: {task_dir}", flush=True)
        return

    shutil.rmtree(task_dir, ignore_errors=True)
    print(f"[image_store] 🗑️ 已删除任务目录: {task_dir}", flush=True)
