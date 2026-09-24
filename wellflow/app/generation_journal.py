"""Durable per-image results. TaskEvent is the journal; files are batch-scoped."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from sqlalchemy import select
from wellflow.app.database import session_scope
from wellflow.app.models.task_models import TaskEvent


def generation_id(state, work_items):
    data = {"revision": state.get("workflow_revision", 0), "items": work_items,
            "refs": (state.get("node3") or {}).get("reference_images"),
            "products": (state.get("request") or {}).get("product_images")}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]


def prepare_batch(task_id, node4, revision):
    with session_scope() as db:
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id,
                            TaskEvent.event_type == "generation_prepared")).all()
        if any(e.payload_json.get("generation_id") == node4["generation_id"] for e in events):
            return
        payload = {k: node4[k] for k in ("generation_id", "work_items")}
        payload["revision"] = revision
        db.add(TaskEvent(task_id=task_id, event_type="generation_prepared", payload_json=payload))
        db.commit()


def persist_image(task_id, batch_id, output):
    # Save before publishing. Remote URLs can expire; materialize them as well.
    from wellflow.app.config import settings
    import base64
    import httpx
    import io
    import os
    from PIL import Image
    url = output["image_url"]
    if url.startswith("data:"):
        raw = base64.b64decode(url.split(",", 1)[1], validate=True)
    elif url.startswith(("http://", "https://")):
        response = httpx.get(url, timeout=120, follow_redirects=True, trust_env=False)
        response.raise_for_status()
        raw = response.content
    else:
        raise ValueError("生图返回了不支持的图片地址")
    with Image.open(io.BytesIO(raw)) as image:
        ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(image.format)
        image.verify()
    if not ext:
        raise ValueError("生成图片格式无效")
    digest = hashlib.sha256(raw).hexdigest()
    relative = f"{task_id}/outputs/{batch_id}-{digest}.{ext}"
    dest = Path(settings.upload_dir) / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".tmp")
    with temporary.open("wb") as file:
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(dest)
    stable_url = "/uploads/" + relative
    saved = {**output, "image_url": stable_url,
             "image_key": hashlib.sha256(stable_url.encode()).hexdigest()}
    with session_scope() as db:
        db.add(TaskEvent(task_id=task_id, event_type="generation_image_done",
                         payload_json={"generation_id": batch_id, "output": saved}))
        db.commit()
    return saved


def restore_completed(task_id, node4, revision=None):
    result = dict(node4 or {})
    with session_scope() as db:
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id,
                            TaskEvent.event_type.in_(["generation_prepared", "generation_image_done"]))
                            .order_by(TaskEvent.event_id)).all()
    if not result.get("generation_id") and revision is not None:
        batches = [e.payload_json for e in events if e.event_type == "generation_prepared"
                   and e.payload_json.get("revision") == revision]
        if batches:
            result.update(batches[-1])
    batch = result.get("generation_id")
    if not batch:
        return result
    saved = {o["work_item_id"]: o for o in result.get("outputs", [])}
    for event in events:
        if event.event_type == "generation_image_done" and event.payload_json.get("generation_id") == batch:
            output = event.payload_json["output"]
            saved[output["work_item_id"]] = output
    result["work_items"] = [{**item, "status": "done"} if item["work_item_id"] in saved else dict(item)
                            for item in result.get("work_items", [])]
    result["outputs"] = sorted(saved.values(), key=lambda o: o.get("prompt_index", 0))
    result["completed_count"] = len(saved)
    return result
