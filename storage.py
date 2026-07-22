"""
Slip file storage - local disk (default, unchanged local/LAN behavior) or Supabase
Storage (when SUPABASE_URL + SUPABASE_SERVICE_KEY are set, for cloud deployment where
the local filesystem doesn't persist across restarts).
"""
import os
import uuid

import requests
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_SLIP_EXT = {"png", "jpg", "jpeg", "webp", "gif", "pdf"}

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "slips")

USE_CLOUD = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

if not USE_CLOUD:
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_SLIP_EXT


def _content_type_for(ext):
    return {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif", "pdf": "application/pdf",
    }.get(ext, "application/octet-stream")


def save_slip(file_storage):
    """Save an uploaded slip. Returns the stored filename, "INVALID_TYPE", or None (no file)."""
    if not file_storage or file_storage.filename == "":
        return None
    filename = secure_filename(file_storage.filename)
    if not allowed_file(filename):
        return "INVALID_TYPE"
    ext = filename.rsplit(".", 1)[1].lower()
    stored_name = f"{uuid.uuid4().hex}.{ext}"

    if USE_CLOUD:
        resp = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{stored_name}",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "apikey": SUPABASE_SERVICE_KEY,
                "Content-Type": _content_type_for(ext),
            },
            data=file_storage.read(),
            timeout=30,
        )
        resp.raise_for_status()
    else:
        file_storage.save(os.path.join(UPLOAD_DIR, stored_name))

    return stored_name


def delete_slip(filename):
    if not filename:
        return
    if USE_CLOUD:
        try:
            requests.delete(
                f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}",
                headers={"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "apikey": SUPABASE_SERVICE_KEY},
                timeout=30,
            )
        except requests.RequestException:
            pass  # best-effort cleanup - a failed delete here shouldn't break the user's request
    else:
        path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(path):
            os.remove(path)


def slip_url(filename):
    """Public URL to view a slip. Cloud mode: direct Supabase public URL. Local mode: our own /uploads route."""
    if USE_CLOUD:
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    return f"/uploads/{filename}"
