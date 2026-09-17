"""WellFlow 统一图片上传接口。

两种模式（由 session_id 区分，优先 session_id）：
  - **通用素材库模式**（不传 session_id）
      路径：uploads/assets/{upload_id}/i0.jpg ...
      用途：SKU 建档、其他素材库上传；先调此接口拿 storage_uri，
            再把 storage_uri 喂给 SkuCreateRequest.images[] 等字段
  - **模特 session 模式**（传 session_id）
      路径：uploads/{session_id}/i0.jpg ...
      用途：模特创建流程的参考图上传；传 session_id 后后续
            /generate /fine-tune /auto-tag 都能基于同一个 session 目录

统一替代：
  - 原来的 POST /api/reference/mannequins/upload（已标记 deprecated，
    内部改为调此接口复用同一落盘逻辑）

路由：POST /api/wellflow/image/uploads
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from wellflow.app.api.utils import ok, StandardResponse
from wellflow.app.utils.image_store import save_asset_upload


router = APIRouter(prefix="/wellflow/image", tags=["WellFlow 通用上传"])


# 常见图片 MIME + 扩展名白名单
_ALLOWED_MIME_PREFIXES = ("image/",)
_MAX_FILE_SIZE = 20 * 1024 * 1024  # 单张 20MB
_MAX_FILES = 20


class UploadedImageInfo(BaseModel):
    index: int
    original_name: str
    filename: str
    storage_uri: str
    url: str
    mime: str
    size: int
    width: int = 0
    height: int = 0


class UploadResult(BaseModel):
    upload_id: str
    count: int
    images: list[UploadedImageInfo]


@router.post(
    "/uploads",
    summary="统一图片上传（通用素材库 / 模特 session 共用）",
    description=(
        "两种模式：\n\n"
        "1. **通用素材库**（不传 session_id）—— 文件存到 `uploads/assets/{upload_id}/`，"
        "返回的 `storage_uri` 可直接喂给 SKU 创建等表单接口。\n\n"
        "2. **模特 session**（传 session_id）—— 文件存到 `uploads/{session_id}/`，"
        "返回的 `storage_uri` 可传给模特创建流程的 `/generate` `/auto-tag` 等端点。"
    ),
    response_model=StandardResponse[UploadResult],
)
async def upload_images(
    files: Annotated[list[UploadFile], File(..., description="图片文件列表（支持多张，最多 20 张）")],
    session_id: str | None = Query(
        default=None,
        description="可选：模特创建流程的 session_id（任意唯一字符串）；"
                    "传了就存 uploads/{session_id}/，不传存 uploads/assets/{upload_id}/",
    ),
) -> dict:
    if not files:
        raise HTTPException(400, "files 不能为空")

    if len(files) > _MAX_FILES:
        raise HTTPException(400, f"单次最多上传 {_MAX_FILES} 张，当前 {len(files)} 张")

    raw_pairs: list[tuple[str, bytes, str | None]] = []
    for f in files:
        # MIME 校验
        content_type = f.content_type or ""
        if not content_type.startswith(_ALLOWED_MIME_PREFIXES):
            raise HTTPException(400, f"文件 {f.filename} 不是图片（mime={content_type or '未知'}）")

        raw = await f.read()

        # 大小校验
        if len(raw) > _MAX_FILE_SIZE:
            raise HTTPException(
                400,
                f"文件 {f.filename} 超过 {_MAX_FILE_SIZE // 1024 // 1024}MB 上限（{len(raw) // 1024 // 1024}MB）",
            )

        raw_pairs.append((f.filename or "image", raw, content_type))

    dir_id, images_info = save_asset_upload(raw_pairs, session_id=session_id)

    result = UploadResult(
        upload_id=dir_id,
        count=len(images_info),
        images=[UploadedImageInfo(**item) for item in images_info],
    )
    return ok(result.model_dump())
