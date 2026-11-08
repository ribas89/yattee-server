"""Persistent playback-first video response cache.

Stores safe, validated metadata. Cached complete media is served through newly
signed local cache URLs. Never reuse expired YouTube / relay URLs for partials.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Awaitable, Callable, Optional

import config
import metadata_cache
import video_cache
from models import VideoResponse

logger = logging.getLogger(__name__)
_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
_MAX_BYTES = 3 * 1024 * 1024
_jobs: dict[str, asyncio.Task] = {}
_semaphore = asyncio.Semaphore(2)


def _path(video_id: str) -> Optional[Path]:
    if not _ID.fullmatch(video_id):
        return None
    return Path(config.VIDEO_INFO_CACHE_DIR) / f'{video_id}.json'


def _read_record(video_id: str) -> Optional[dict]:
    entry = metadata_cache._read(_path(video_id), _MAX_BYTES)
    if not entry or entry.get('video_id') != video_id or not isinstance(entry.get('response'), dict):
        return None
    return entry


def store(video_id: str, response: VideoResponse) -> bool:
    """Store a complete, *nonlive* video metadata snapshot atomically.

    Signed media URLs reside only in this private metadata file and are checked
    for expiry on each read. Never publish a failed extraction over a good one.
    """
    if response.liveNow or response.isUpcoming or response.videoId != video_id:
        return False
    record = response.model_dump(mode='json')
    # Do not persist optional request-derived credentials/headers.
    for fmt in record.get('formatStreams', []) + record.get('adaptiveFormats', []):
        fmt.pop('httpHeaders', None)
    old = _read_record(video_id)
    if old:
        prior = old['response']
        for key in ('recommendedVideos', 'description', 'descriptionHtml', 'authorThumbnails', 'storyboards', 'captions'):
            if not record.get(key) and prior.get(key):
                record[key] = prior[key]
    ok = metadata_cache._write(_path(video_id), {
        'video_id': video_id,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'response': record,
    }, _MAX_BYTES)
    if ok:
        logger.info('[VideoInfoCache] STORED %s (%d recommendations)', video_id,
                    len(record.get('recommendedVideos') or []))
    return ok


def last_refresh(video_id: str) -> float:
    entry = _read_record(video_id)
    if not entry:
        return 0.0
    try:
        return datetime.fromisoformat(entry['fetched_at']).timestamp()
    except (KeyError, ValueError, TypeError):
        return 0.0


def _valid_upstream_url(url: str) -> bool:
    """Allow fresh signed relay URLs only; never trust persisted expired URLs."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.path != '/proxy/relay':
            return False  # media cache only supports signed relay mode
        q = urllib.parse.parse_qs(parsed.query)
        exp = int(q['exp'][0])
        if exp <= time.time() + 120:
            return False
        upstream = urllib.parse.parse_qs(urllib.parse.urlsplit(q['url'][0]).query)
        if 'expire' in upstream and int(upstream['expire'][0]) <= time.time() + 120:
            return False
        return bool(q.get('sig'))
    except (KeyError, ValueError, TypeError, IndexError):
        return False


def load(video_id: str, base_url: str) -> Optional[VideoResponse]:
    """Return a playable cached response without any network request.

    Completed formats get new URLs tied to the persistent local media cache.
    Partials are usable only while their stored upstream signature is fresh.
    """
    entry = _read_record(video_id)
    if not entry:
        return None
    data = entry['response'].copy()
    if data.get('liveNow') or data.get('isUpcoming'):
        return None
    from routers.proxy._relay import signed_cached_url

    playable = 0
    for field in ('formatStreams', 'adaptiveFormats'):
        formats = []
        for fmt in data.get(field) or []:
            if not isinstance(fmt, dict):
                continue
            fmt = fmt.copy()
            fid = str(fmt.get('itag') or '')
            if video_cache.lookup(video_id, fid):
                fmt['url'] = signed_cached_url(base_url, video_id, fid)
                playable += 1
                formats.append(fmt)
            elif _valid_upstream_url(fmt.get('url') or ''):
                playable += 1
                formats.append(fmt)
        data[field] = formats
    # Do not let stale manifest/segment URLs mislead clients.
    data['hlsUrl'] = None
    data['dashUrl'] = None
    # Caption URLs may be signed, and also need fresh extraction to refresh.
    data['captions'] = []
    if not playable:
        logger.info('[VideoInfoCache] STALE-STREAM %s (no playable formats)', video_id)
        return None
    try:
        response = VideoResponse.model_validate(data)
    except (ValueError, TypeError) as exc:
        logger.warning('[VideoInfoCache] Invalid cached response for %s: %s', video_id, exc)
        return None
    logger.info('[VideoInfoCache] HIT %s (%d formats)', video_id, playable)
    return metadata_cache.apply_description(video_id, response)


def schedule(video_id: str, factory: Callable[[], Awaitable[None]]) -> bool:
    """Launch deduplicated best-effort background revalidation (never awaited by request)."""
    active = _jobs.get(video_id)
    if active is not None and not active.done():
        return False
    async def runner():
        try:
            async with _semaphore:
                await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('[VideoInfoCache] Background refresh failed for %s: %s', video_id, exc)
        finally:
            _jobs.pop(video_id, None)
    task = asyncio.create_task(runner())
    _jobs[video_id] = task
    return True


def due(video_id: str) -> bool:
    minutes = config.VIDEO_INFO_REFRESH_MINUTES
    return minutes == 0 or time.time() - last_refresh(video_id) >= 60 * minutes
