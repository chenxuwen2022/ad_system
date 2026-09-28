"""Generate bounded previews of local public images without fetching remote URLs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError


def thumbnail_path(source: str, size: int, roots: dict[str, Path], cache: Path) -> Path:
    path = None
    for prefix, directory in roots.items():
        if source.startswith(prefix):
            root = directory.resolve()
            candidate = (root / source[len(prefix):]).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                path = candidate
            break
    if path is None:
        raise HTTPException(404, "图片不存在或不支持生成缩略图")
    stat = path.stat()
    key = hashlib.sha256(f"v1:{path}:{stat.st_mtime_ns}:{stat.st_size}:{size}".encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{key}.webp"
    if target.is_file():
        return target
    temporary = None
    try:
        with Image.open(path) as original:
            preview = ImageOps.exif_transpose(original)
            preview.thumbnail((size, size))
            preview = preview.convert("RGBA" if "A" in preview.getbands() else "RGB")
            with tempfile.NamedTemporaryFile(dir=cache, suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                preview.save(output, format="WEBP", quality=78)
            os.replace(temporary, target)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, "无法生成该图片的缩略图") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
