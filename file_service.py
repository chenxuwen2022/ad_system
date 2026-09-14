import os
import shutil
from config import MEDIA_STORAGE_PATH


def save_upload_file(file_obj, filename: str) -> str:
    dest_path = os.path.join(MEDIA_STORAGE_PATH, filename)
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file_obj, f)
    return dest_path


def delete_media_file(file_path: str):
    if os.path.exists(file_path):
        os.remove(file_path)
        return True
    return False
