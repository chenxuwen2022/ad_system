"""Archive only explicitly selected C4 outputs, independently of conversation files.

No output-id column is needed: persisted C4 events validate the selection and a
content-addressed SKU path provides retry deduplication under the SKU row lock.
A task event is the durable receipt when checkpoint completion needs a retry.
"""
from __future__ import annotations

from wellflow.app.logging import log_message

import base64
import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from wellflow.app.models.asset_models import ProductSku, ProductImage
from wellflow.app.models.task_models import Task, TaskEvent, TaskImage
from wellflow.app.repositories.conversation_repo import ConversationRepo
from wellflow.app.schemas.asset_schemas import ArchiveGeneratedImages



def image_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()



def with_image_keys(payload: dict) -> dict:
    """Add transport-only selection keys, including to pre-migration history."""
    if not isinstance(payload.get("outputs"), list):
        return payload
    return {**payload, "outputs": [
        {**output, "image_key": image_key(output["image_url"])} if output.get("image_url") else output
        for output in payload["outputs"]
    ]}


def save_ad_image(sku_id: int, url: str) -> str:
    from PIL import Image
    from wellflow.app.config import settings
    import httpx

    root = Path(settings.upload_dir).resolve()
    limit = 30 * 1024 * 1024
    if url.startswith("data:image/"):
        if len(url) > limit * 4 // 3 + 256:
            raise ValueError("图片超过 30 MB")
        raw = base64.b64decode(url.split(",", 1)[1], validate=True)
    elif url.startswith(("https://", "http://")):
        # URL comes exclusively from a validated server-side generation event.
        with httpx.stream("GET", url, timeout=45, follow_redirects=True) as response:
            response.raise_for_status()
            buf = bytearray()
            for chunk in response.iter_bytes():
                buf.extend(chunk)
                if len(buf) > limit:
                    raise ValueError("图片超过 30 MB")
            raw = bytes(buf)
    else:
        relative = url.lstrip("/")
        if not relative.startswith("uploads/"):
            raise ValueError("生成图片路径无效")
        path = (root / relative.removeprefix("uploads/")).resolve()
        if root not in path.parents:
            raise ValueError("生成图片路径无效")
        if not path.is_file():
            raise HTTPException(409, "生成图片文件在当前服务中不存在，无法入库。请确认生图和入库使用同一服务，或检查图片存储是否共享")
        if path.stat().st_size > limit:
            raise ValueError("生成图片路径或大小无效")
        raw = path.read_bytes()
    if not raw or len(raw) > limit:
        raise ValueError("生成图片为空或超过 30 MB")
    with Image.open(io.BytesIO(raw)) as img:
        ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(img.format)
        img.verify()
    if not ext:
        raise ValueError("仅支持 PNG、JPEG、WebP 图片")
    name = hashlib.sha256(raw).hexdigest() + "." + ext
    directory = root / "sku" / str(sku_id) / "ad"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    if not destination.exists():
        # Atomic replacement: interrupted writes never become valid asset files.
        import tempfile
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(raw)
        try:
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)
    return f"uploads/sku/{sku_id}/ad/{name}"


def prepare_archive(db: Session, sku_id: int, request: ArchiveGeneratedImages) -> dict:
    conv = ConversationRepo(db).resolve(request.conversation_id)
    if not conv or conv.sku_id != sku_id:
        raise HTTPException(409, "对话关联的 SKU 与入库商品不一致，请先绑定商品")
    task = db.scalar(select(Task).where(Task.task_id == request.task_id).with_for_update())
    if not task or task.conversation_id != conv.conversation_id:
        raise HTTPException(409, "任务不属于当前对话")
    selected = sorted({(item.task_id, item.image_key) for item in request.images})
    # Receipts track completion; the live SKU associations determine deduplication.
    receipt = db.scalar(select(TaskEvent).where(
        TaskEvent.task_id == task.task_id, TaskEvent.event_type == "sku_images_archived",
    ).order_by(TaskEvent.event_id.desc()))
    if task.phase not in ("c4_review", "archive_pending", "done"):
        raise HTTPException(409, "当前任务不在图片确认阶段")
    if conv.current_task_id != task.task_id:
        raise HTTPException(409, "请在当前任务中确认入库")
    from sqlalchemy.orm import lazyload
    sku = db.scalar(
        select(ProductSku)
        .where(ProductSku.id == sku_id)
        .options(lazyload(ProductSku.brand), lazyload(ProductSku.series))
        .with_for_update()
    )
    if not sku:
        raise HTTPException(404, "商品不存在")
    source_ids = {source for source, _ in selected}
    tasks = db.scalars(select(Task).where(Task.task_id.in_(source_ids))).all()
    if len(tasks) != len(source_ids) or any(t.conversation_id != conv.conversation_id for t in tasks):
        raise HTTPException(409, "所选图片不属于当前对话")
    candidates = {}
    events = db.scalars(select(TaskEvent).where(
        TaskEvent.task_id.in_(source_ids), TaskEvent.event_type == "graph_interrupt_c4",
    ).order_by(TaskEvent.event_id)).all()
    for event in events:
        for output in (event.payload_json or {}).get("outputs", []):
            url = output.get("image_url")
            if url:
                candidates[(event.task_id, image_key(url))] = output
    # Current interrupt is also authoritative, even if event persistence was interrupted.
    for source in tasks:
        for output in (source.interrupt_json or {}).get("outputs", []):
            url = output.get("image_url")
            if url:
                candidates[(source.task_id, image_key(url))] = output
    if any(key not in candidates for key in selected):
        raise HTTPException(422, "所选图片无法在生成记录中找到，请刷新后重试")
    existing = {img.storage_uri: img for img in db.scalars(select(ProductImage).where(
        ProductImage.sku_id == sku_id, ProductImage.image_type == "ad",
    ))}
    previous = (receipt.payload_json or {}) if receipt and (receipt.payload_json or {}).get("sku_id") == sku_id else {}
    saved = [item for item in previous.get("images", [])
             if item["storage_uri"] in existing]
    selection = {tuple(item) for item in previous.get("selection", [])}
    selection.update(selected)
    added_count = 0
    skipped_count = 0
    order = max((img.sort_order for img in existing.values()), default=-1) + 1
    for source_id, key in selected:
        output = candidates[(source_id, key)]
        # A validated receipt can resolve an existing association even if its
        # original generation URL has expired. Removed associations are restored.
        known = next((item for item in saved if item.get("shot_id") == key), None)
        uri = known["storage_uri"] if known else save_ad_image(sku_id, output["image_url"])
        img = existing.get(uri)
        if img is None:
            img = ProductImage(sku_id=sku_id, image_type="ad", category="其他",
                               source_task_id=source_id, storage_uri=uri, sort_order=order)
            db.add(img)
            db.flush()
            existing[uri] = img
            order += 1
            added_count += 1
        else:
            skipped_count += 1
        if not any(item["product_image_id"] == img.id for item in saved):
            saved.append({"product_image_id": img.id, "storage_uri": uri,
                          "prompt": output.get("prompt"), "prompt_index": output.get("prompt_index"),
                          "shot_id": key})
    payload = {"sku_id": sku_id, "selection": [list(item) for item in sorted(selection)], "images": saved,
               "added_count": added_count, "skipped_count": skipped_count}
    payload["message"] = f"入库完成：新增关联 {added_count} 张，已关联跳过 {skipped_count} 张"
    archive_event = TaskEvent(task_id=task.task_id, event_type="sku_images_archived", payload_json=payload)
    db.add(archive_event)
    db.flush()
    payload = {**payload, "archive_event_id": archive_event.event_id}
    sku.updated_at = datetime.now(timezone.utc)
    if task.phase != "done":
        task.phase = "archive_pending"
    db.commit()
    return payload


def complete_archive(db: Session, task_id: str, receipt: dict) -> None:
    task = db.scalar(select(Task).where(Task.task_id == task_id).with_for_update())
    if not task:
        raise HTTPException(404, "任务不存在")
    for img in receipt["images"]:
        image_id = hashlib.sha256(f'{task_id}:{img["storage_uri"]}'.encode()).hexdigest()
        if db.get(TaskImage, image_id) is None:
            db.add(TaskImage(image_id=image_id, task_id=task_id, image_type="output",
                             storage_uri=img["storage_uri"], shot_id=img["shot_id"],
                             prompt=img["prompt"], prompt_index=img["prompt_index"]))
    # Preserve outputs that only reached the interrupt before clearing it.
    if (task.interrupt_json or {}).get("outputs"):
        db.add(TaskEvent(task_id=task_id, event_type="graph_interrupt_c4",
                         payload_json=task.interrupt_json))
    task.phase = "done"
    task.interrupt_json = None
    task.updated_at = datetime.now(timezone.utc)
    summary = {
        "phase": "done", "output_count": len(receipt["images"]),
        "image_paths": [img["storage_uri"] for img in receipt["images"]],
        "prompts": [img["prompt"] for img in receipt["images"]],
    }
    done_event = db.scalar(select(TaskEvent).where(
        TaskEvent.task_id == task_id, TaskEvent.event_type == "workflow_done",
    ).order_by(TaskEvent.event_id.desc()))
    if done_event:
        done_event.payload_json = summary
    else:
        db.add(TaskEvent(task_id=task_id, event_type="workflow_done", phase="done", payload_json=summary))
    archive_event = db.get(TaskEvent, receipt.get("archive_event_id")) if receipt.get("archive_event_id") else None
    if archive_event and not (archive_event.payload_json or {}).get("result_recorded"):
        db.add(TaskEvent(task_id=task_id, event_type="sku_archive_result", payload_json={
            "message": receipt["message"], "sku_id": receipt["sku_id"],
            "added_count": receipt["added_count"], "skipped_count": receipt["skipped_count"],
        }))
        archive_event.payload_json = {**archive_event.payload_json, "result_recorded": True}
    db.commit()


async def archive_generated_images(sku_id: int, request: ArchiveGeneratedImages) -> dict:
    import asyncio
    from langgraph.types import Command
    from wellflow.app.database import session_scope
    from wellflow.app.runtime import get_graph

    from wellflow.app.workflow_execution import execution_lease
    from wellflow.app.graph_persist import reconcile_checkpoint
    async with execution_lease(request.task_id):
        try:
            graph = get_graph()
            if graph is None:
                raise RuntimeError("工作流暂不可用")
            config = {"configurable": {"thread_id": request.task_id}}
            snapshot = await graph.aget_state(config)
            await asyncio.to_thread(reconcile_checkpoint, request.task_id, snapshot)
            def prepare():
                with session_scope() as db:
                    receipt = prepare_archive(db, sku_id, request)
                    return receipt, db.get(Task, request.task_id).phase == "done"
            receipt, already_done = await asyncio.to_thread(prepare)
            from wellflow.app.workflow_status import checkpoint_view
            phase, interrupt = checkpoint_view(snapshot)
            if not already_done and phase != "done":
                if not interrupt or interrupt["node"] != "c4":
                    raise RuntimeError("工作流不在图片确认节点")
                result = await graph.ainvoke(Command(resume={"decision": "confirm", "revision": interrupt.get("revision")}), config=config)
                if result.get("phase") != "done":
                    raise RuntimeError("工作流尚未完成")
            def finish():
                with session_scope() as db:
                    complete_archive(db, request.task_id, receipt)
            await asyncio.to_thread(finish)
            return receipt
        except HTTPException:
            raise
        except Exception as exc:
            log_message(("SKU image archive failed: sku_id=%s task_id=%s conversation_id=%s" % (sku_id, request.task_id, request.conversation_id,)), page='sku商品库', business='商品图片归档', status='失败', exc_info=True)
            raise HTTPException(503, "图片入库处理暂未完成，请重试；已关联到该商品的图片会自动跳过") from exc
