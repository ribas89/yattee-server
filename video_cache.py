"""Persistent, sparse byte-range cache for signed relay media streams.

Cache entries are keyed by YouTube video ID and format ID, *not* ephemeral
GoogleVideo URLs. Completed files and individually verified partial byte ranges are reusable.
Missing ranges are fetched from upstream on demand, never read from sparse holes. Cleanup is intentionally restricted to this cache directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)
_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{11}:[0-9]{1,5}(?:-[A-Za-z0-9_]{1,32}){0,3}$")
_ENTRY_VERSION = 1
# Global in-process metadata lock. os.pwrite writes from concurrent requests
# use disjoint file descriptors and explicit offsets.
_lock = threading.RLock()
_active: dict[str, int] = {}
_COMMIT_EVERY = 4 * 1024 * 1024
_ORPHAN_MAX_AGE_SECONDS = 24 * 60 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_seconds(raw) -> Optional[float]:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def _prefix(video_id: str, format_id: str) -> str:
    key = f"{video_id}:{format_id}"
    if not _KEY_RE.fullmatch(key):
        raise ValueError("Invalid video cache key")
    return f"{video_id}_{format_id}"


def _paths(video_id: str, format_id: str) -> tuple[Path, Path, Path]:
    root = Path(config.VIDEO_CACHE_DIR)
    prefix = _prefix(video_id, format_id)
    return root / f"{prefix}.json", root / f"{prefix}.part", root / f"{prefix}.media"


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == _ENTRY_VERSION:
            return data
    except (ValueError, OSError, UnicodeError):
        pass
    return None


def _atomic_json(path: Path, data: dict) -> None:
    # Only one writer can replace the index at once; process-local lock held.
    # Use thread ID so concurrent processes/threads don't share a temporary.
    temp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _merge(existing: list, start: int, end: int) -> list[list[int]]:
    result = []
    for lo, hi in sorted([*existing, [start, end]]):
        if not result or lo > result[-1][1]:
            result.append([lo, hi])
        else:
            result[-1][1] = max(result[-1][1], hi)
    return result


@dataclass(frozen=True)
class CachedVideo:
    path: Path
    size: int
    content_type: str


@dataclass(frozen=True)
class CachedPartial:
    """Snapshot of durable ranges [start, end), read without trusting holes."""
    path: Path
    size: int
    content_type: str
    ranges: tuple[tuple[int, int], ...]


def lookup(video_id: str, format_id: str) -> Optional[CachedVideo]:
    """Return fully cached file, refreshing its last-use time for expiration."""
    if config.VIDEO_CACHE_DAYS == 0:
        return None
    try:
        index, _, media = _paths(video_id, format_id)
    except ValueError:
        return None
    with _lock:
        meta = _read_json(index)
        if not meta or not meta.get("completed_at"):
            return None
        size = meta.get("size")
        if not isinstance(size, int) or size <= 0:
            return None
        try:
            if not media.is_file() or media.stat().st_size != size:
                return None
        except OSError:
            return None
        last = _iso_seconds(meta.get("last_accessed_at")) or 0
        if time.time() - last >= 600:
            meta["last_accessed_at"] = _now_iso()
            try:
                _atomic_json(index, meta)
            except OSError as exc:
                logger.warning("[VideoCache] Could not update access time: %s", exc)
        return CachedVideo(media, size, meta.get("content_type") or "application/octet-stream")


def _valid_ranges(value: object, size: int) -> Optional[tuple[tuple[int, int], ...]]:
    if not isinstance(value, list) or len(value) > 100_000:
        return None
    previous = 0
    intervals = []
    for item in value:
        if (not isinstance(item, list) or len(item) != 2
                or any(type(x) is not int for x in item)):
            return None
        lo, hi = item
        if lo < previous or hi <= lo or hi > size:
            return None
        previous = hi
        intervals.append((lo, hi))
    return tuple(intervals)


def lookup_partial(video_id: str, format_id: str, expected_size: Optional[int] = None) -> Optional[CachedPartial]:
    """Return persisted partial intervals (not holes) after validating metadata.

    Access time is also refreshed so frequently watched *incomplete* videos
    remain available for the configured retention period.
    """
    if config.VIDEO_CACHE_DAYS == 0:
        return None
    try:
        index, part, _ = _paths(video_id, format_id)
    except ValueError:
        return None
    with _lock:
        meta = _read_json(index)
        if not meta or meta.get("completed_at"):
            return None
        size = meta.get("size")
        if type(size) is not int or size <= 0 or (expected_size is not None and size != expected_size):
            return None
        intervals = _valid_ranges(meta.get("ranges"), size)
        if not intervals:
            return None
        try:
            if not part.is_file() or part.stat().st_size < intervals[-1][1]:
                return None
        except OSError:
            return None
        last = _iso_seconds(meta.get("last_accessed_at")) or 0
        if time.time() - last >= 600:
            meta["last_accessed_at"] = _now_iso()
            try:
                _atomic_json(index, meta)
            except OSError as exc:
                logger.warning("[VideoCache] Could not update partial access time: %s", exc)
        return CachedPartial(part, size, meta.get("content_type") or "application/octet-stream", intervals)


def split_ranges(snapshot: CachedPartial, start: int, end_exclusive: int) -> list[tuple[bool, int, int]]:
    """Return a contiguous ordered partition into cached and missing spans."""
    if start < 0 or end_exclusive <= start or end_exclusive > snapshot.size:
        raise ValueError("Invalid cached range partition")
    result: list[tuple[bool, int, int]] = []
    cursor = start
    for lo, hi in snapshot.ranges:
        if hi <= cursor:
            continue
        if lo >= end_exclusive:
            break
        if cursor < lo:
            result.append((False, cursor, min(lo, end_exclusive)))
            cursor = min(lo, end_exclusive)
        if cursor < end_exclusive and cursor < hi:
            result.append((True, cursor, min(hi, end_exclusive)))
            cursor = min(hi, end_exclusive)
    if cursor < end_exclusive:
        result.append((False, cursor, end_exclusive))
    return result


def cached_partial_response(video_id: str, format_id: str, client_range: Optional[str]):
    """Serve a requested span entirely from disk, otherwise return None.

    An open-ended Range cannot be served from a cached *prefix* alone: its
    contract is to return all remaining media bytes, including missing ones.
    """
    partial = lookup_partial(video_id, format_id)
    if partial is None:
        return None
    bounds = parse_client_range(client_range, partial.size)
    if bounds is None:
        return None  # Let ordinary endpoint return a 416 for malformed ranges.
    start, end = bounds
    segments = split_ranges(partial, start, end + 1)
    if len(segments) == 1 and segments[0][0]:
        return cached_response(CachedVideo(partial.path, partial.size, partial.content_type),
                               client_range, cache_state="PARTIAL-HIT")
    return None


def parse_client_range(value: Optional[str], size: int) -> Optional[tuple[int, int]]:
    if not value:
        return 0, size - 1
    text = value.strip()
    normal = re.fullmatch(r"bytes=(\d+)-(\d*)", text)
    suffix = re.fullmatch(r"bytes=-(\d+)", text)
    if normal:
        start = int(normal.group(1))
        end = min(int(normal.group(2)), size - 1) if normal.group(2) else size - 1
    elif suffix and int(suffix.group(1)) > 0:
        start, end = max(0, size - int(suffix.group(1))), size - 1
    else:
        return None
    if start >= size or end < start:
        return None
    return start, end


def read_cached_chunks(snapshot: CachedPartial, start: int, end_exclusive: int):
    """Read exactly one verified cached region; avoid .part->.media rename races."""
    with _lock:
        path = snapshot.path
        if not path.exists():
            path = snapshot.path.with_suffix(".media")
        fd = os.open(path, os.O_RDONLY)
    try:
        cursor = start
        while cursor < end_exclusive:
            block = os.pread(fd, min(1024 * 1024, end_exclusive - cursor), cursor)
            if not block:
                raise OSError("Cached media range was truncated")
            cursor += len(block)
            yield block
    finally:
        os.close(fd)


def _new_metadata(video_id: str, format_id: str, size: int, content_type: str) -> dict:
    return {
        "version": _ENTRY_VERSION,
        "video_id": video_id,
        "format_id": format_id,
        "size": size,
        "content_type": content_type,
        "ranges": [],
        "created_at": _now_iso(),
        "last_accessed_at": _now_iso(),
        "completed_at": None,
    }


class CacheWriter:
    """Accumulates verified contiguous ranges from one HTTP relay response."""

    def __init__(self, key: str, index: Path, part: Path, media: Path, fd: int, size: int, offset: int):
        self._key = key
        self._index = index
        self._part = part
        self._media = media
        self._fd = fd
        self._size = size
        self._offset = offset
        self._pending_start = offset
        self._bytes_since_commit = 0
        self._written_total = 0
        self._closed = False
        self._failed = False

    def write(self, chunk: bytes) -> None:
        if self._closed or self._failed or not chunk:
            return
        if self._offset + len(chunk) > self._size:
            self._failed = True
            raise ValueError("Stream exceeds declared media size")
        try:
            view = memoryview(chunk)
            while view:
                n = os.pwrite(self._fd, view, self._offset)
                if n <= 0:
                    raise OSError("Cache disk write returned zero bytes")
                self._offset += n
                view = view[n:]
            self._bytes_since_commit += len(chunk)
            self._written_total += len(chunk)
            if self._bytes_since_commit >= _COMMIT_EVERY:
                self._commit()
        except OSError:
            self._failed = True
            raise

    def _commit(self) -> None:
        if self._failed or self._offset == self._pending_start:
            return
        os.fsync(self._fd)  # metadata may only claim bytes already durable
        with _lock:
            meta = _read_json(self._index)
            if not meta or meta.get("size") != self._size or meta.get("completed_at"):
                return
            ranges = meta.get("ranges")
            if not isinstance(ranges, list):
                ranges = []
            meta["ranges"] = _merge(ranges, self._pending_start, self._offset)
            meta["last_accessed_at"] = _now_iso()
            _atomic_json(self._index, meta)
        self._pending_start = self._offset
        self._bytes_since_commit = 0

    def close(self) -> None:
        if self._closed:
            return
        try:
            if not self._failed:
                self._commit()
        except (OSError, ValueError) as exc:
            logger.warning("[VideoCache] Failed to finish caching chunk: %s", exc)
        finally:
            os.close(self._fd)
            self._closed = True
            if self._written_total > 0:
                logger.info("[VideoCache] SAVED %s bytes=%d dir=%s", self._key,
                            self._written_total, self._index.parent)
            with _lock:
                _active[self._key] = max(0, _active.get(self._key, 1) - 1)
                if _active[self._key] == 0:
                    _active.pop(self._key, None)
                    self._publish_if_complete()

    def _publish_if_complete(self) -> None:
        meta = _read_json(self._index)
        if not meta or meta.get("size") != self._size or meta.get("completed_at"):
            return
        if meta.get("ranges") != [[0, self._size]]:
            return
        try:
            if not self._part.exists() or self._part.stat().st_size != self._size:
                return
            os.replace(self._part, self._media)
            meta["completed_at"] = _now_iso()
            meta["last_accessed_at"] = meta["completed_at"]
            meta.pop("ranges", None)
            _atomic_json(self._index, meta)
            logger.info("[VideoCache] Completed %s (%d bytes)", self._key, self._size)
        except OSError as exc:
            logger.warning("[VideoCache] Failed to publish completed video %s: %s", self._key, exc)


def begin(video_id: str, format_id: str, size: Optional[int], content_type: str, offset: int) -> Optional[CacheWriter]:
    """Create writer for one media byte range; never cache unknown-size/live."""
    if config.VIDEO_CACHE_DAYS == 0 or not isinstance(size, int) or size <= 0:
        return None
    if offset < 0 or offset >= size or not content_type.startswith(("video/", "audio/")):
        return None
    try:
        index, part, media = _paths(video_id, format_id)
    except ValueError:
        return None
    key = f"{video_id}:{format_id}"
    with _lock:
        if lookup(video_id, format_id):
            return None
        index.parent.mkdir(parents=True, exist_ok=True)
        meta = _read_json(index)
        if meta and meta.get("size") != size:
            # Do not clobber another running writer on a changed file.
            if _active.get(key, 0):
                return None
            index.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            media.unlink(missing_ok=True)
            meta = None
        if meta and meta.get("completed_at"):
            # Complete metadata without matching file: invalid entry.
            if _active.get(key, 0):
                return None
            index.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            media.unlink(missing_ok=True)
            meta = None
        if meta is None:
            _atomic_json(index, _new_metadata(video_id, format_id, size, content_type))
        fd = os.open(part, os.O_WRONLY | os.O_CREAT, 0o600)
        _active[key] = _active.get(key, 0) + 1
        return CacheWriter(key, index, part, media, fd, size, offset)


def cleanup_expired() -> tuple[int, int]:
    """Startup cleanup only. Delete expired complete videos and old partials.

    Last playback access determines the configured retention window for both
    completed files and partially watched videos. Unindexed orphan files
    are discarded after 24 hours; other cache directories aren't touched.
    """
    root = Path(config.VIDEO_CACHE_DIR)
    if not root.is_dir():
        return 0, 0
    now = time.time()
    removed_complete = removed_partial = 0
    with _lock:
        for index in root.glob("*.json"):
            prefix = index.stem
            if not re.fullmatch(r"[a-zA-Z0-9_-]{11}_[0-9]{1,5}(?:-[A-Za-z0-9_]{1,32}){0,3}", prefix):
                continue
            meta = _read_json(index)
            if meta is None:
                continue
            key = f"{meta.get('video_id')}:{meta.get('format_id')}"
            if _active.get(key, 0):
                continue
            complete = bool(meta.get("completed_at"))
            age = now - (_iso_seconds(meta.get("last_accessed_at")) or index.stat().st_mtime)
            threshold = config.VIDEO_CACHE_DAYS * 86400
            if age < threshold:
                continue
            for suffix in (".media", ".part", ".json"):
                (root / f"{prefix}{suffix}").unlink(missing_ok=True)
            if complete:
                removed_complete += 1
            else:
                removed_partial += 1
        # Sweep abandoned media/partial files with no valid index. An
        # interrupted atomic publish can leave a .media file without metadata.
        for candidate in (*root.glob("*.part"), *root.glob("*.media")):
            if not re.fullmatch(r"[a-zA-Z0-9_-]{11}_[0-9]{1,5}(?:-[A-Za-z0-9_]{1,32}){0,3}", candidate.stem):
                continue
            if _read_json(root / f"{candidate.stem}.json") is not None:
                continue
            if now - candidate.stat().st_mtime >= _ORPHAN_MAX_AGE_SECONDS:
                candidate.unlink(missing_ok=True)
                removed_partial += 1
    logger.info(
        "[VideoCache] Startup cleanup: removed %d expired videos and %d incomplete entries (retention=%d days)",
        removed_complete, removed_partial, config.VIDEO_CACHE_DAYS,
    )
    return removed_complete, removed_partial


def cached_response(cached: CachedVideo, client_range: Optional[str], cache_state: str = "HIT"):
    """Serve a complete cache hit with HTTP Range semantics and no upstream I/O."""
    from fastapi.responses import Response, StreamingResponse

    size = cached.size
    ranged = client_range is not None
    bounds = parse_client_range(client_range, size)
    if bounds is None:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = bounds

    headers = {
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
        "X-Yattee-Video-Cache": cache_state,
    }
    if ranged:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    async def content():
        # A concurrent request can complete this partial and atomically rename
        # .part -> .media after the cache hit was selected. Open under the
        # same lock as publish, falling back to the final path if renamed.
        with _lock:
            try:
                fd = os.open(cached.path, os.O_RDONLY)
            except FileNotFoundError:
                if cached.path.suffix != ".part":
                    raise
                fd = os.open(cached.path.with_suffix(".media"), os.O_RDONLY)
        try:
            at = start
            while at <= end:
                chunk = os.pread(fd, min(1024 * 1024, end - at + 1), at)
                if not chunk:
                    raise OSError("Cached file unexpectedly truncated during playback")
                at += len(chunk)
                yield chunk
        finally:
            os.close(fd)

    return StreamingResponse(
        content(), status_code=206 if ranged else 200,
        headers=headers, media_type=cached.content_type,
    )
