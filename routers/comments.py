"""Comments endpoints - InnerTube primary, Invidious fallback."""

import asyncio
import logging
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query

import innertube
import invidious_proxy
import metadata_cache
from converters import resolve_invidious_url

router = APIRouter(tags=["comments"])
logger = logging.getLogger(__name__)
_refresh_jobs = {}


def _resolve_comment_thumbnails(comments: List[Any], invidious_base: str) -> List[Any]:
    """Resolve relative thumbnail URLs in comment author thumbnails."""
    resolved_comments = []
    for comment in comments:
        resolved_comment = dict(comment)

        # Resolve author thumbnails
        if "authorThumbnails" in resolved_comment:
            resolved_thumbs = []
            for thumb in resolved_comment["authorThumbnails"]:
                resolved_thumb = dict(thumb)
                if "url" in resolved_thumb:
                    resolved_thumb["url"] = resolve_invidious_url(resolved_thumb["url"], invidious_base)
                resolved_thumbs.append(resolved_thumb)
            resolved_comment["authorThumbnails"] = resolved_thumbs

        # Recursively resolve replies if present
        if "replies" in resolved_comment and resolved_comment["replies"]:
            if "comments" in resolved_comment["replies"]:
                resolved_comment["replies"]["comments"] = _resolve_comment_thumbnails(
                    resolved_comment["replies"]["comments"], invidious_base
                )

        resolved_comments.append(resolved_comment)

    return resolved_comments


@router.get("/comments/{video_id}")
async def get_comments(
    video_id: str, continuation: Optional[str] = Query(None, description="Continuation token for pagination")
):
    """
    Get comments for a video.

    Tries InnerTube first (direct YouTube API), falls back to Invidious if configured.
    Responds 404 when the video owner turned comments off so clients can show a
    dedicated message instead of a generic error.
    """
    cached = metadata_cache.get_comments(video_id, continuation)
    if cached is not None:
        logger.debug("[Comments] Cache hit for %s", video_id)
        return cached

    # Stale-while-revalidate: comments never block replay of a previously
    # visited video. A failed refresh does not erase cached comments.
    stale = metadata_cache.get_comments_stale(video_id, continuation)
    if stale is not None:
        key = (video_id, continuation)
        task = _refresh_jobs.get(key)
        if task is None or task.done():
            async def refresh():
                try:
                    await _get_comments_live(video_id, continuation)
                except Exception as exc:
                    logger.info('[Comments] Background refresh failed for %s: %s', video_id, exc)
                finally:
                    _refresh_jobs.pop(key, None)
            _refresh_jobs[key] = asyncio.create_task(refresh())
        return stale

    return await _get_comments_live(video_id, continuation)


async def _get_comments_live(video_id: str, continuation: Optional[str]):
    """Network-only fetch for first access or a background refresh."""
    # Try InnerTube first
    empty_innertube = None
    try:
        data = await innertube.get_comments(video_id, continuation)
        if data and data.get("comments"):
            logger.info(f"[Comments] InnerTube success for {video_id}: {len(data['comments'])} comments")
            metadata_cache.store_comments(video_id, continuation, data)
            return data
        if data and isinstance(data.get("comments"), list):
            empty_innertube = data
        logger.debug(f"[Comments] InnerTube returned no comments for {video_id}, trying Invidious")
    except innertube.CommentsDisabledError:
        raise HTTPException(status_code=404, detail="Comments are disabled")
    except innertube.InnerTubeError as e:
        logger.debug(f"[Comments] InnerTube error for {video_id}: {e}")

    # Fall back to Invidious
    if invidious_proxy.is_enabled():
        try:
            data = await invidious_proxy.get_comments(video_id, continuation)
            if data is None:
                return {"comments": [], "continuation": None}

            # Resolve relative URLs in comment author thumbnails
            if "comments" in data:
                invidious_base = invidious_proxy.get_base_url()
                data["comments"] = _resolve_comment_thumbnails(data["comments"], invidious_base)

            metadata_cache.store_comments(video_id, continuation, data)
            return data
        except invidious_proxy.InvidiousProxyError as e:
            if e.status_code == 404:
                # Invidious answers "Comments not found." for videos with comments turned off
                raise HTTPException(status_code=404, detail="Comments are disabled")
            raise HTTPException(status_code=502, detail=f"Comments proxy error: {e}")

    # An authenticated InnerTube empty result is worth caching too.
    if empty_innertube is not None:
        metadata_cache.store_comments(video_id, continuation, empty_innertube)
        return empty_innertube
    # Neither source available — return empty rather than 503.
    return {"comments": [], "continuation": None}
