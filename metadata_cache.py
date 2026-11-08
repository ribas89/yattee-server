"""Minimal file-backed caches for video descriptions and comments.

Only metadata is written here. Never cache signed playback or HLS URLs.
"""

from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Optional

import config

logger = logging.getLogger(__name__)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_MAX_DESCRIPTION_BYTES = 256 * 1024
_MAX_COMMENTS_BYTES = 4 * 1024 * 1024


def _file(root: str, video_id: str, continuation: Optional[str] = None) -> Optional[Path]:
    if not _VIDEO_ID.fullmatch(video_id):
        return None
    key = json.dumps([video_id, continuation], separators=(",", ":"), ensure_ascii=True)
    return Path(root) / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


def _read(path: Optional[Path], size_limit: int) -> Optional[dict]:
    if path is None:
        return None
    try:
        if path.stat().st_size > size_limit:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write(path: Optional[Path], payload: dict, size_limit: int) -> bool:
    if path is None:
        return False
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > size_limit:
        return False
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".metadata-", dir=path.parent, delete=False) as handle:
            tmp = handle.name
            handle.write(encoded)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("[MetadataCache] Could not write cache: %s", exc)
        return False
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def apply_description(video_id: str, response):
    """Freeze the first available nonempty description, without saving stream URLs."""
    path = _file(config.VIDEO_DESCRIPTION_CACHE_DIR, video_id)
    cached = _read(path, _MAX_DESCRIPTION_BYTES)
    if cached and cached.get("video_id") == video_id and isinstance(cached.get("description"), str):
        response.description = cached["description"]
        if isinstance(cached.get("description_html"), str):
            response.descriptionHtml = cached["description_html"]
        return response

    if response.description and isinstance(response.description, str):
        _write(path, {
            "video_id": video_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "description": response.description,
            "description_html": response.descriptionHtml,
        }, _MAX_DESCRIPTION_BYTES)
    return response


def get_comments(video_id: str, continuation: Optional[str]) -> Optional[dict]:
    minutes = config.COMMENTS_CACHE_MINUTES
    if minutes <= 0:
        return None
    path = _file(config.COMMENTS_CACHE_DIR, video_id, continuation)
    cached = _read(path, _MAX_COMMENTS_BYTES)
    if not cached or cached.get("video_id") != video_id or cached.get("continuation") != continuation:
        return None
    try:
        stamped = datetime.fromisoformat(cached["fetched_at"])
        if stamped.tzinfo is None:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    age = datetime.now(timezone.utc) - stamped.astimezone(timezone.utc)
    data = cached.get("data")
    if timedelta(0) <= age < timedelta(minutes=minutes) and isinstance(data, dict) and isinstance(data.get("comments"), list):
        return data
    return None


def get_comments_stale(video_id: str, continuation: Optional[str]) -> Optional[dict]:
    """Last-known comments, even when TTL expired. Used for instant SWR reads."""
    if config.COMMENTS_CACHE_MINUTES <= 0:
        return None
    cached = _read(_file(config.COMMENTS_CACHE_DIR, video_id, continuation), _MAX_COMMENTS_BYTES)
    if not cached or cached.get('video_id') != video_id or cached.get('continuation') != continuation:
        return None
    data = cached.get('data')
    return data if isinstance(data, dict) and isinstance(data.get('comments'), list) else None


def store_comments(video_id: str, continuation: Optional[str], data: dict) -> None:
    if config.COMMENTS_CACHE_MINUTES <= 0:
        return
    if not isinstance(data, dict) or not isinstance(data.get("comments"), list):
        return
    _write(_file(config.COMMENTS_CACHE_DIR, video_id, continuation), {
        "video_id": video_id,
        "continuation": continuation,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }, _MAX_COMMENTS_BYTES)
