"""Persistent, on-demand channel avatar cache.

Only channel page/video endpoints may initiate YouTube avatar lookups.
Ordinary /feed and /avatar requests use files stored under AVATAR_CACHE_DIR.
"""

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

import config
import innertube
import invidious_proxy
from converters import resolve_invidious_url
from security import is_safe_url_strict

logger = logging.getLogger('avatar_cache')
MAX_IMAGE_BYTES = 2 * 1024 * 1024
RETRY_FAILURE_AFTER = 3600


def _paths(channel_id: str) -> tuple[Path, Path]:
    key = hashlib.sha256(channel_id.encode('utf-8')).hexdigest()
    root = Path(config.AVATAR_CACHE_DIR)
    return root / f'{key}.json', root / f'{key}.img'


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.avatar-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@dataclass
class CachedAvatar:
    channel_id: str
    thumbnails: List[Dict[str, Any]]
    cached_at: float
    last_attempt_at: float = 0.0
    image_content_type: str = 'image/jpeg'

    def is_expired(self) -> bool:
        return time.time() - self.cached_at >= config.AVATAR_REFRESH_DAYS * 86400


class AvatarCache:
    def __init__(self):
        self._cache: Dict[str, CachedAvatar] = {}
        self._pending: set[str] = set()
        self._scheduled: set[str] = set()
        self._image_pending: set[str] = set()
        self._fetch_semaphore = asyncio.Semaphore(1)

    def _read_cached(self, channel_id: str) -> Optional[CachedAvatar]:
        if channel_id in self._cache:
            return self._cache[channel_id]
        metadata_path, _ = _paths(channel_id)
        try:
            data = json.loads(metadata_path.read_text('utf-8'))
            if data.get('channel_id') != channel_id:
                return None
            entry = CachedAvatar(
                channel_id=channel_id,
                thumbnails=data.get('thumbnails') or [],
                cached_at=float(data.get('cached_at') or 0),
                last_attempt_at=float(data.get('last_attempt_at') or 0),
                image_content_type=data.get('image_content_type') or 'image/jpeg',
            )
            self._cache[channel_id] = entry
            return entry
        except (OSError, ValueError, TypeError):
            return None

    def _save(self, entry: CachedAvatar) -> None:
        metadata_path, _ = _paths(entry.channel_id)
        data = {
            'channel_id': entry.channel_id,
            'thumbnails': entry.thumbnails,
            'cached_at': entry.cached_at,
            'last_attempt_at': entry.last_attempt_at,
            'image_content_type': entry.image_content_type,
        }
        _atomic_write(metadata_path, json.dumps(data, separators=(',', ':')).encode())
        self._cache[entry.channel_id] = entry

    def _should_fetch(self, channel_id: str) -> bool:
        entry = self._read_cached(channel_id)
        if entry and entry.cached_at and not entry.is_expired():
            return False
        if entry and time.time() - entry.last_attempt_at < RETRY_FAILURE_AFTER:
            return False
        return True

    async def get(self, channel_id: str) -> Optional[List[Dict[str, Any]]]:
        """Read cache only. Never schedule a network request here."""
        entry = self._read_cached(channel_id)
        return entry.thumbnails if entry and entry.thumbnails else None

    def get_cached_image(self, channel_id: str) -> Optional[tuple[bytes, str]]:
        entry = self._read_cached(channel_id)
        _, image_path = _paths(channel_id)
        if not entry:
            return None
        try:
            return image_path.read_bytes(), entry.image_content_type
        except OSError:
            return None

    def store_from_channel_info(self, channel_id: str, thumbnails: object) -> bool:
        """Called only on a channel visit, with thumbnails already retrieved."""
        if not isinstance(thumbnails, list):
            return False
        valid = [t for t in thumbnails if isinstance(t, dict) and t.get('url')]
        if not valid:
            return False
        entry = self._read_cached(channel_id)
        if entry and not entry.is_expired() and entry.cached_at:
            # Ensure image exists, but never reset the 14-day freshness clock.
            self._schedule_image(channel_id, entry.thumbnails)
            return True
        now = time.time()
        try:
            self._save(CachedAvatar(channel_id, valid, now, now,
                                    entry.image_content_type if entry else 'image/jpeg'))
        except OSError as exc:
            logger.warning('[AvatarCache] Cannot persist metadata: %s', exc)
            return False
        self._schedule_image(channel_id, valid, replace=True)
        return True

    def _schedule_image(self, channel_id: str, thumbs: list, replace: bool = False) -> None:
        _, image_path = _paths(channel_id)
        if not replace and image_path.exists():
            return
        if channel_id in self._image_pending:
            return
        self._image_pending.add(channel_id)
        try:
            asyncio.get_running_loop().create_task(self._download_image(channel_id, thumbs))
        except RuntimeError:
            self._image_pending.discard(channel_id)

    async def _download_image(self, channel_id: str, thumbs: list) -> None:
        try:
            thumb = max(thumbs, key=lambda t: t.get('width') or t.get('height') or 0)
            url = str(thumb['url'])
            if url.startswith('//'):
                url = 'https:' + url
            safe, reason = is_safe_url_strict(url)
            if not safe:
                logger.warning('[AvatarCache] Unsafe image URL for %s: %s', channel_id, reason)
                return
            async with self._fetch_semaphore:
                async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
                    async with client.stream('GET', url) as resp:
                        resp.raise_for_status()
                        content_type = resp.headers.get('content-type', '').split(';')[0].strip().lower()
                        if content_type not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
                            logger.warning('[AvatarCache] Unexpected image MIME: %s', content_type)
                            return
                        content = bytearray()
                        async for piece in resp.aiter_bytes():
                            content.extend(piece)
                            if len(content) > MAX_IMAGE_BYTES:
                                logger.warning('[AvatarCache] Oversize avatar for %s', channel_id)
                                return
                if not content:
                    return
                _, image_path = _paths(channel_id)
                _atomic_write(image_path, bytes(content))
                entry = self._read_cached(channel_id)
                if entry:
                    entry.image_content_type = content_type
                    self._save(entry)
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            logger.warning('[AvatarCache] Failed to cache image for %s: %s', channel_id, exc)
        finally:
            self._image_pending.discard(channel_id)

    async def fetch_and_cache(self, channel_id: str) -> Optional[List[Dict[str, Any]]]:
        """Only invoked by channel page/videos endpoints."""
        entry = self._read_cached(channel_id)
        if not self._should_fetch(channel_id):
            if entry:
                self._schedule_image(channel_id, entry.thumbnails)
            return entry.thumbnails if entry else None
        if channel_id in self._pending:
            return entry.thumbnails if entry else None
        self._pending.add(channel_id)
        try:
            thumbnails = None
            try:
                info = await innertube.get_channel_info(channel_id)
                thumbnails = info.get('authorThumbnails') if info else None
            except innertube.InnerTubeError:
                pass
            if not thumbnails and invidious_proxy.is_enabled():
                try:
                    data = await invidious_proxy.get_channel(channel_id)
                    base = invidious_proxy.get_base_url()
                    thumbnails = [
                        {**t, 'url': resolve_invidious_url(t.get('url', ''), base)}
                        for t in (data or {}).get('authorThumbnails', [])
                    ]
                except invidious_proxy.InvidiousProxyError:
                    pass
            if thumbnails:
                self.store_from_channel_info(channel_id, thumbnails)
                return thumbnails
        except Exception as exc:
            logger.warning('[AvatarCache] Channel refresh failed for %s: %s', channel_id, exc)
        finally:
            self._pending.discard(channel_id)
        # Persist failure time without losing the previous working image.
        entry = self._read_cached(channel_id) or CachedAvatar(channel_id, [], 0)
        entry.last_attempt_at = time.time()
        try:
            self._save(entry)
        except OSError:
            pass
        return entry.thumbnails or None

    def schedule_background_fetch(self, channel_id: str) -> None:
        """Use only from the channel page/videos endpoints."""
        if channel_id in self._scheduled or channel_id in self._pending or not self._should_fetch(channel_id):
            return
        self._scheduled.add(channel_id)
        try:
            asyncio.get_running_loop().create_task(self._background_fetch(channel_id))
        except RuntimeError:
            self._scheduled.discard(channel_id)

    async def _background_fetch(self, channel_id: str) -> None:
        try:
            async with self._fetch_semaphore:
                await self.fetch_and_cache(channel_id)
        finally:
            self._scheduled.discard(channel_id)

    def cleanup_expired(self) -> None:
        """Keep stale disk entries as fallback; only evict RAM copies."""
        self._cache.clear()

    def stats(self) -> Dict[str, Any]:
        return {
            'total_entries': len(self._cache),
            'expired_entries': sum(1 for a in self._cache.values() if a.is_expired()),
            'pending_fetches': len(self._pending),
            'ttl_seconds': config.AVATAR_REFRESH_DAYS * 86400,
            'max_size': 10000,
        }


_avatar_cache: Optional[AvatarCache] = None
_cleanup_task: Optional[asyncio.Task] = None
CLEANUP_INTERVAL = 3600


def get_cache() -> AvatarCache:
    global _avatar_cache
    if _avatar_cache is None:
        _avatar_cache = AvatarCache()
    return _avatar_cache


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        get_cache().cleanup_expired()


def start_avatar_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info('[AvatarCache] Persistent lazy cache enabled (days=%d, path=%s)',
                    config.AVATAR_REFRESH_DAYS, config.AVATAR_CACHE_DIR)


def stop_avatar_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
