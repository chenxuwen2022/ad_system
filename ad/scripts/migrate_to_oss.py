# -*- coding: utf-8 -*-
"""存量素材迁移到 OSS：
- media_storage/*.图片/视频 -> OSS media/<fname>
- media_storage/thumb/*.jpg -> OSS thumb/<fname>
本地文件保留不动（投放依赖本地文件流）。
幂等：重复运行只传缺失的，已存在的跳过。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ad.services import oss_client

VALID_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
             ".mp4", ".mov", ".avi", ".mkv", ".webm"}


def main():
    if not oss_client.enabled():
        print("[skip] OSS 未配置，不迁移")
        return

    media_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "media_storage")
    if not os.path.isdir(media_dir):
        print(f"[skip] 目录不存在: {media_dir}")
        return

    uploaded = skipped = failed = 0
    t0 = time.time()

    # 1) 原图/原视频
    for fn in os.listdir(media_dir):
        fp = os.path.join(media_dir, fn)
        if not os.path.isfile(fp):
            continue
        if os.path.splitext(fn)[1].lower() not in VALID_EXT:
            continue
        key = oss_client.media_key(fn)
        if oss_client.object_exists(key):
            skipped += 1
            continue
        ok = oss_client.upload_file(fp, key)
        if ok:
            uploaded += 1
            print(f"  [media] {fn} ({os.path.getsize(fp)//1024}KB)")
        else:
            failed += 1
            print(f"  [FAIL]  {fn}")

    # 2) 缩略图（本地 thumb/ 子目录）
    thumb_dir = os.path.join(media_dir, "thumb")
    if os.path.isdir(thumb_dir):
        for fn in os.listdir(thumb_dir):
            fp = os.path.join(thumb_dir, fn)
            if not os.path.isfile(fp) or not fn.lower().endswith(".jpg"):
                continue
            key = oss_client.KEY_THUMB + fn  # thumb/<stem>.jpg
            if oss_client.object_exists(key):
                skipped += 1
                continue
            ok = oss_client.upload_file(fp, key)
            if ok:
                uploaded += 1
            else:
                failed += 1
                print(f"  [FAIL thumb] {fn}")

    oss_client.invalidate_thumb_cache()
    print(f"\n[done] uploaded={uploaded} skipped={skipped} failed={failed} "
          f"cost={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
