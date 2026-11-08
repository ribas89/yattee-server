"""Background feed fetcher for subscribed channels."""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

import httpx

import avatar_cache
import config
import database
import innertube
import invidious_proxy
from converters import parse_relative_time, resolve_invidious_url
from security import is_safe_url_strict
from settings import get_settings
from ytdlp_wrapper import YtDlpError, get_channel_info, is_valid_url, run_ytdlp

logger = logging.getLogger(__name__)

_fetch_task: Optional[asyncio.Task] = None

# On-demand feed refresh. A second POST /feed may queue *due* channels,
# but never overrides the persisted FEED_REFRESH_MINUTES freshness window.
# Only one job per channel is active at a time, with bounded concurrency.
_due_jobs: dict[tuple[str, str], asyncio.Task] = {}
_due_last_attempt: dict[tuple[str, str], float] = {}
_due_semaphore = asyncio.Semaphore(2)


def schedule_due_refresh(channels: list[dict], *, skip_keys=None) -> int:
    """Schedule only stale channels from this feed; return cached data immediately.

    ``last_fetch`` comes from the persistent database, so restarts do not
    change which channels are eligible. Already-running jobs are deduplicated.
    ``skip_keys`` excludes channels whose initial fetch is pending.
    """
    interval = _feed_interval_seconds()
    now = datetime.now(UTC)
    wanted = {(c["channel_id"], c["site"]): c for c in channels}
    skip = set(skip_keys or ())
    watched = database.get_all_watched_channels()
    due = []
    fresh = 0
    running = 0
    cooldown = 0
    for record in watched:
        key = record["channel_id"], record["site"]
        if key not in wanted or key in skip:
            continue
        existing = _due_jobs.get(key)
        if existing is not None and not existing.done():
            running += 1
            continue
        if _is_recent_feed(record.get("last_fetch"), interval, now):
            fresh += 1
            continue
        # A failed upstream fetch must not cause another YouTube fetch on
        # every client poll. Back off for up to five minutes before retrying.
        if time.monotonic() - _due_last_attempt.get(key, -1e9) < min(interval, 300):
            cooldown += 1
            continue
        due.append((key, wanted[key]))

    for key, channel in due:
        _due_last_attempt[key] = time.monotonic()
        async def worker(item=channel, channel_key=key):
            try:
                async with _due_semaphore:
                    # Recheck freshness *after* waiting in the queue: another
                    # fetcher may have refreshed this channel in the meantime.
                    current = next((ch for ch in database.get_all_watched_channels()
                                    if (ch["channel_id"], ch["site"]) == channel_key), None)
                    if current and _is_recent_feed(current.get("last_fetch"),
                                                   _feed_interval_seconds(), datetime.now(UTC)):
                        return
                    await fetch_single_channel(item["channel_id"], item["site"], item.get("channel_url"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[Feed] On-demand refresh failed for %s: %s", channel_key, exc)
            finally:
                _due_jobs.pop(channel_key, None)

        _due_jobs[key] = asyncio.create_task(worker())

    logger.info("[Feed] SECOND-REQUEST queued=%d fresh=%d already-running=%d cooldown=%d interval=%dmin",
                len(due), fresh, running, cooldown, interval // 60)
    return len(due)



def _feed_interval_seconds() -> int:
    if config.FEED_REFRESH_MINUTES is not None:
        return config.FEED_REFRESH_MINUTES * 60
    return get_settings().feed_fetch_interval


def _is_recent_feed(last_fetch: Optional[str], interval: int, now: datetime) -> bool:
    if not last_fetch:
        return False
    try:
        previous = datetime.fromisoformat(str(last_fetch).replace("Z", "+00:00"))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        age_seconds = (now - previous).total_seconds()
        return 0 <= age_seconds < interval
    except (TypeError, ValueError, OverflowError):
        return False


_FEED_FETCH_ERRORS = (
    YtDlpError,
    invidious_proxy.InvidiousProxyError,
    httpx.RequestError,
    json.JSONDecodeError,
    sqlite3.Error,
    OSError,
    ValueError,
    KeyError,
)


def _build_channel_url(channel_id: str, site: str, channel_url: str) -> str:
    """Build the URL for fetching channel videos.

    Args:
        channel_id: The channel ID
        site: The site/platform name
        channel_url: The stored channel URL

    Returns:
        URL string for fetching

    Raises:
        ValueError: If channel_url is required but not provided, or if URL is invalid
    """
    if site.lower() == "youtube":
        if channel_id.startswith("@"):
            return f"https://www.youtube.com/{channel_id}/videos"
        elif channel_id.startswith("UC"):
            return f"https://www.youtube.com/channel/{channel_id}/videos"

    # For non-YouTube sites, channel_url is required
    if not channel_url:
        raise ValueError(f"channel_url is required for {site} channels (channel_id: {channel_id})")

    # Validate user-provided URL to prevent command injection
    if not is_valid_url(channel_url):
        raise ValueError(f"Invalid channel URL format: {channel_url}")

    # SSRF prevention - validate URL doesn't target internal resources
    # DNS resolution enabled to prevent DNS rebinding attacks
    is_safe, reason = is_safe_url_strict(channel_url, resolve_dns=True)
    if not is_safe:
        raise ValueError(f"Channel URL blocked ({reason}): {channel_url}")

    return channel_url


def _process_invidious_video(v: dict, channel_id: str, invidious_base: str) -> dict:
    """Convert a single Invidious video to our cached format.

    Args:
        v: Invidious video data
        channel_id: The channel ID
        invidious_base: Invidious instance base URL for resolving thumbnails

    Returns:
        Video dict in our format
    """
    # Resolve relative thumbnail URLs before processing
    raw_thumbnails = v.get("videoThumbnails", [])
    resolved_thumbnails = []
    for thumb in raw_thumbnails:
        resolved_thumb = thumb.copy()
        resolved_thumb["url"] = resolve_invidious_url(thumb.get("url", ""), invidious_base)
        resolved_thumbnails.append(resolved_thumb)

    # Extract all thumbnails and best URL
    best_thumb_url, all_thumbnails = _get_all_thumbnails(resolved_thumbnails)

    return {
        "video_id": v.get("videoId", ""),
        "title": v.get("title", ""),
        "author": v.get("author", ""),
        "author_id": v.get("authorId", channel_id),
        "length_seconds": v.get("lengthSeconds", 0),
        "view_count": v.get("viewCount"),
        "published": v.get("published"),
        "published_text": v.get("publishedText", ""),
        "thumbnail_url": best_thumb_url,
        "thumbnails": all_thumbnails,
        "video_url": f"https://www.youtube.com/watch?v={v.get('videoId', '')}",
    }


def _process_ytdlp_video(info: dict, channel_id: str) -> dict:
    """Convert a single yt-dlp video to our cached format.

    Args:
        info: yt-dlp video info dict
        channel_id: The channel ID

    Returns:
        Video dict in our format
    """
    best_thumb_url, all_thumbnails = _get_all_ytdlp_thumbnails(info)

    return {
        "video_id": info.get("id", ""),
        "title": info.get("title", ""),
        "author": info.get("channel") or info.get("uploader") or "",
        "author_id": info.get("channel_id") or info.get("uploader_id") or channel_id,
        "length_seconds": info.get("duration") or 0,
        "view_count": info.get("view_count"),
        "published": _parse_timestamp(info.get("timestamp") or info.get("upload_date")),
        "published_text": info.get("upload_date") or "",
        "thumbnail_url": best_thumb_url,
        "thumbnails": all_thumbnails,
        "video_url": info.get("url") or info.get("webpage_url") or "",
    }


def _parse_relative_time(text: str) -> Optional[int]:
    """Parse relative time text like '2 days ago' into an approximate Unix timestamp.

    Returns None if the text can't be parsed.
    """
    return parse_relative_time(text)


def _process_innertube_video(v: dict, channel_id: str) -> dict:
    """Convert an InnerTube video (Invidious-compatible format) to our cached format.

    Args:
        v: InnerTube video data (from video_renderer_to_invidious)
        channel_id: The channel ID

    Returns:
        Video dict in our format
    """
    # InnerTube thumbnails are already absolute URLs
    raw_thumbnails = v.get("videoThumbnails", [])
    best_thumb_url, all_thumbnails = _get_all_thumbnails(raw_thumbnails)

    return {
        "video_id": v.get("videoId", ""),
        "title": v.get("title", ""),
        "author": v.get("author", ""),
        "author_id": v.get("authorId", channel_id),
        "length_seconds": v.get("lengthSeconds", 0),
        "view_count": v.get("viewCount"),
        "published": _parse_relative_time(v.get("publishedText", "")),
        "published_text": v.get("publishedText", ""),
        "thumbnail_url": best_thumb_url,
        "thumbnails": all_thumbnails,
        "video_url": f"https://www.youtube.com/watch?v={v.get('videoId', '')}",
    }


async def _fetch_channel_metadata_innertube(channel_id: str) -> Optional[Dict[str, Any]]:
    """Fetch channel metadata from InnerTube.

    Args:
        channel_id: The channel ID

    Returns:
        Dict with subscriber_count and is_verified, or None
    """
    try:
        channel_info = await innertube.get_channel_info(channel_id)
        if channel_info:
            return {
                "subscriber_count": channel_info.get("subCount"),
                "is_verified": channel_info.get("authorVerified", False),
                "avatar_thumbnails": channel_info.get("authorThumbnails") or [],
            }
    except innertube.InnerTubeError as e:
        logger.debug(f"[Feed] InnerTube channel metadata failed for {channel_id}: {e}")
    except (KeyError, TypeError, ValueError) as e:
        logger.debug(f"[Feed] InnerTube channel metadata failed for {channel_id}: {e}")
    return None


async def _fetch_from_innertube(
    channel_id: str, max_videos: int
) -> tuple[Optional[List[dict]], Optional[Dict[str, Any]]]:
    """Fetch videos from InnerTube API.

    Args:
        channel_id: The YouTube channel ID
        max_videos: Maximum number of videos to fetch

    Returns:
        Tuple of (videos_list or None, channel_metadata or None)
    """
    try:
        # Fetch channel info first to get channel name (videos on channel tab lack author info)
        channel_info = None
        channel_name = ""
        try:
            channel_info = await innertube.get_channel_info(channel_id)
            if channel_info:
                channel_name = channel_info.get("author", "")
        except innertube.InnerTubeError:
            pass

        all_videos = []
        continuation = None
        page_count = 0

        while len(all_videos) < max_videos:
            page_count += 1
            data = await innertube.get_channel_videos(channel_id, continuation)

            if not data or not data.get("videos"):
                break

            for v in data["videos"]:
                # Fill in author/authorId if missing (channel tab videos don't include them)
                if not v.get("author"):
                    v["author"] = channel_name
                if not v.get("authorId"):
                    v["authorId"] = channel_id
                all_videos.append(_process_innertube_video(v, channel_id))

            continuation = data.get("continuation")
            if not continuation:
                break

        if not all_videos:
            return None, None

        all_videos = all_videos[:max_videos]
        logger.info(f"[Feed] InnerTube: fetched {len(all_videos)} videos for {channel_id} ({page_count} pages)")

        # Build channel metadata from already-fetched channel info
        channel_metadata = None
        if channel_info:
            channel_metadata = {
                "subscriber_count": channel_info.get("subCount"),
                "is_verified": channel_info.get("authorVerified", False),
                "avatar_thumbnails": channel_info.get("authorThumbnails") or [],
            }

        return all_videos, channel_metadata

    except innertube.InnerTubeError as e:
        logger.info(f"[Feed] InnerTube failed for {channel_id}: {e}")
        return None, None
    except (KeyError, TypeError, ValueError) as e:
        logger.debug(f"[Feed] InnerTube failed for {channel_id}: {e}")
        return None, None


async def _fetch_channel_metadata_invidious(channel_id: str) -> Optional[Dict[str, Any]]:
    """Fetch channel metadata from Invidious.

    Args:
        channel_id: The channel ID

    Returns:
        Dict with subscriber_count and is_verified, or None
    """
    try:
        channel_info = await invidious_proxy.get_channel(channel_id)
        if channel_info:
            return {
                "subscriber_count": channel_info.get("subCount"),
                "is_verified": channel_info.get("authorVerified", False),
                "avatar_thumbnails": channel_info.get("authorThumbnails") or [],
            }
    except (KeyError, TypeError, ValueError) as e:
        logger.debug(f"Failed to fetch channel info for {channel_id}: {e}")
    return None


async def _fetch_channel_metadata_ytdlp(channel_id: str) -> Optional[Dict[str, Any]]:
    """Fetch channel metadata from yt-dlp.

    Args:
        channel_id: The channel ID

    Returns:
        Dict with subscriber_count and is_verified, or None
    """
    try:
        info = await get_channel_info(channel_id)
        return {
            "subscriber_count": info.get("channel_follower_count"),
            "is_verified": info.get("channel_is_verified", False),
        }
    except (YtDlpError, KeyError, TypeError, ValueError) as e:
        logger.debug(f"Failed to fetch channel info for {channel_id}: {e}")
    return None


async def _fetch_from_invidious(
    channel_id: str, max_videos: int
) -> tuple[Optional[List[dict]], Optional[Dict[str, Any]], bool, Optional[str]]:
    """Fetch videos from Invidious API.

    Args:
        channel_id: The YouTube channel ID
        max_videos: Maximum number of videos to fetch

    Returns:
        Tuple of (videos_list or None, pagination_info or None, should_fallback_to_ytdlp, fallback_reason or None)
    """
    try:
        result = await invidious_proxy.get_channel_videos_multi_page(channel_id, max_videos)
        if not result or not result.get("videos"):
            return None, None, True, "no_videos"

        s = get_settings()
        # Check if we hit 414 AND fallback is enabled
        if (
            result.get("pagination_limited")
            and result.get("limit_reason") == "414_error"
            and s.feed_fallback_ytdlp_on_414
        ):
            logger.info(
                f"[Feed] {channel_id}: 414 error with {result.get('total_fetched')} videos, "
                f"falling back to yt-dlp for full fetch"
            )
            return None, None, True, "invidious_error_414"

        # Process Invidious results
        raw_video_list = result["videos"]

        # Filter out entries Invidious can't represent as a video. Two known modes:
        #   - parse-error placeholders: {"type": "parse-error", "errorMessage": "..."}
        #   - mis-extracted entries with no videoId (e.g. type="playlist" wrapping
        #     a single video when Invidious can't decode the channel-videos renderer)
        video_list = [v for v in raw_video_list if v.get("type") != "parse-error" and v.get("videoId")]
        dropped = len(raw_video_list) - len(video_list)
        if dropped > 0:
            parse_errors = [v for v in raw_video_list if v.get("type") == "parse-error"]
            no_video_id = [v for v in raw_video_list if v.get("type") != "parse-error" and not v.get("videoId")]
            sample_type = no_video_id[0].get("type") if no_video_id else None
            sample_msg = parse_errors[0].get("errorMessage", "") if parse_errors else ""
            logger.warning(
                f"[Feed] {channel_id}: Invidious returned {dropped} unusable entr(ies) "
                f"out of {len(raw_video_list)} "
                f"(parse_errors={len(parse_errors)}, no_video_id={len(no_video_id)}, "
                f"sample_type={sample_type!r}, sample_error={sample_msg!r})"
            )

        # If Invidious produced no usable videos, fall back to yt-dlp.
        if not video_list:
            return None, None, True, "invidious_unusable_entries"

        pagination_info = {
            "total_fetched": result.get("total_fetched"),
            "pagination_limited": result.get("pagination_limited", False),
            "limit_reason": result.get("limit_reason"),
        }

        invidious_base = invidious_proxy.get_base_url()
        videos = [_process_invidious_video(v, channel_id, invidious_base) for v in video_list]

        return videos, pagination_info, False, None

    except invidious_proxy.InvidiousProxyError as e:
        s = get_settings()
        # Determine fallback reason from error
        if e.status_code:
            fallback_reason = f"invidious_error_{e.status_code}"
        else:
            fallback_reason = "invidious_error_connection"

        # If retryable error occurred (after all retries exhausted) and fallback is enabled
        if e.is_retryable and s.feed_fallback_ytdlp_on_error:
            logger.info(
                f"[Feed] {channel_id}: Invidious failed after retries ({e}), "
                f"falling back to yt-dlp"
            )
            return None, None, True, fallback_reason

        # Non-retryable error or fallback disabled - re-raise
        logger.warning(f"Invidious failed for {channel_id}: {e}")
        raise

    except (KeyError, TypeError, ValueError, OSError) as e:
        logger.warning(f"Invidious failed for {channel_id}, falling back to yt-dlp: {type(e).__name__}: {e}")
        return None, None, True, "invidious_error_other"


async def _fetch_from_ytdlp(
    channel_id: str, site: str, channel_url: str, max_videos: int, use_flat_playlist: bool
) -> tuple[List[dict], Optional[Dict[str, Any]]]:
    """Fetch videos from yt-dlp.

    Args:
        channel_id: The channel ID
        site: The site/platform name
        channel_url: The stored channel URL
        max_videos: Maximum number of videos to fetch
        use_flat_playlist: Whether to use --flat-playlist flag

    Returns:
        Tuple of (videos_list, channel_metadata or None)
    """
    url = _build_channel_url(channel_id, site, channel_url)

    ytdlp_args = ["-j", "--no-warnings", "--playlist-items", f"1:{max_videos}"]
    if use_flat_playlist:
        ytdlp_args.insert(1, "--flat-playlist")

    stdout = await run_ytdlp(*ytdlp_args, url)

    videos = []
    channel_metadata = None

    for line in stdout.strip().split("\n"):
        if not line:
            continue
        try:
            info = json.loads(line)

            # Extract channel metadata from first video only
            if channel_metadata is None:
                channel_metadata = {
                    "subscriber_count": info.get("channel_follower_count"),
                    "is_verified": info.get("channel_is_verified", False),
                }

            videos.append(_process_ytdlp_video(info, channel_id))
        except json.JSONDecodeError:
            continue

    return videos, channel_metadata


async def fetch_channel_feed(
    channel_id: str, site: str, channel_url: str
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Fetch recent videos for a channel.

    Args:
        channel_id: The channel ID
        site: The site/platform name (e.g., "youtube", "dailymotion")
        channel_url: The channel URL for fetching

    Returns:
        Tuple of (videos list, pagination_info dict or None, channel_metadata dict or None)
        pagination_info contains: total_fetched, pagination_limited, limit_reason
        channel_metadata contains: subscriber_count, is_verified
    """
    s = get_settings()
    videos: List[dict] = []
    pagination_info: Optional[Dict[str, Any]] = None
    channel_metadata: Optional[Dict[str, Any]] = None

    fallback_reason: Optional[str] = None

    try:
        # For YouTube: InnerTube → Invidious → yt-dlp
        if site.lower() == "youtube":
            # Try InnerTube first (rich data: viewCount, author, publishedText)
            innertube_videos, innertube_metadata = await _fetch_from_innertube(channel_id, s.feed_max_videos)
            if innertube_videos:
                logger.debug(f"Fetched {len(innertube_videos)} videos from InnerTube for {channel_id}")
                return innertube_videos, None, innertube_metadata

            # Try Invidious second (includes publish timestamps)
            if invidious_proxy.is_enabled():
                logger.debug(f"Attempting Invidious fetch for {channel_id} - max_videos={s.feed_max_videos}")
                videos, pagination_info, should_fallback, fallback_reason = await _fetch_from_invidious(
                    channel_id, s.feed_max_videos
                )

                if videos and not should_fallback:
                    logger.debug(f"Fetched {len(videos)} videos from Invidious for {channel_id}")
                    channel_metadata = await _fetch_channel_metadata_invidious(channel_id)
                    return videos, pagination_info, channel_metadata

        # Fall back to yt-dlp for all sites (or if InnerTube+Invidious failed/not enabled)
        if fallback_reason:
            logger.info(f"[Feed] {channel_id}: Using yt-dlp fallback (reason: {fallback_reason})")
        logger.debug(f"Using yt-dlp for {channel_id} ({site}) - flat_playlist={s.feed_ytdlp_use_flat_playlist}")
        videos, channel_metadata = await _fetch_from_ytdlp(
            channel_id, site, channel_url, s.feed_max_videos, s.feed_ytdlp_use_flat_playlist
        )

        if videos:
            logger.debug(f"Fetched {len(videos)} videos from yt-dlp for {channel_id} ({site})")

    except YtDlpError as e:
        logger.error(f"Failed to fetch channel {channel_id} ({site}): {e}")
        raise
    except (OSError, ValueError) as e:
        logger.error(f"Error fetching channel {channel_id} ({site}): {e}")
        raise

    # If flat-playlist was used, channel_metadata may be None - fetch it separately
    if not channel_metadata and site.lower() == "youtube":
        channel_metadata = await _fetch_channel_metadata_ytdlp(channel_id)

    # yt-dlp doesn't provide pagination metadata
    return videos, pagination_info, channel_metadata


def _get_all_thumbnails(thumbnails: List[dict]) -> tuple[str, List[dict]]:
    """Extract all quality thumbnails and best URL for backwards compatibility.

    Returns:
        Tuple of (best_url, all_thumbnails_list)
    """
    if not thumbnails:
        return "", []

    # Sort thumbnails by quality score (best first)
    quality_scores = {"maxres": 5, "maxresdefault": 5, "sddefault": 4, "high": 3, "medium": 2, "default": 1}

    sorted_thumbs = sorted(thumbnails, key=lambda x: quality_scores.get(x.get("quality", ""), 0), reverse=True)

    # Best URL for legacy thumbnail_url field
    best_url = sorted_thumbs[0].get("url", "") if sorted_thumbs else ""

    return best_url, thumbnails


def _get_all_ytdlp_thumbnails(info: dict) -> tuple[str, List[dict]]:
    """Extract all thumbnails from yt-dlp with quality mapping.

    Returns:
        Tuple of (best_url, all_thumbnails_list)
    """
    thumbnails = info.get("thumbnails", [])
    if not thumbnails:
        fallback_url = info.get("thumbnail", "")
        if fallback_url:
            # Create a minimal thumbnail entry
            return fallback_url, [{"quality": "default", "url": fallback_url, "width": None, "height": None}]
        return "", []

    # Convert to Invidious-compatible format with quality mapping
    result = []
    for thumb in thumbnails:
        width = thumb.get("width", 0)
        height = thumb.get("height", 0)

        # Map dimensions to quality names (Invidious-compatible)
        if width >= 1280:
            quality = "maxres"
        elif width >= 640:
            quality = "sddefault"
        elif width >= 480:
            quality = "high"
        elif width >= 320:
            quality = "medium"
        else:
            quality = "default"

        result.append(
            {
                "quality": quality,
                "url": thumb.get("url", ""),
                "width": width if width else None,
                "height": height if height else None,
            }
        )

    # Sort by width (largest first) and get best URL
    sorted_thumbs = sorted(result, key=lambda x: x.get("width") or 0, reverse=True)
    best_url = sorted_thumbs[0].get("url", "") if sorted_thumbs else ""

    return best_url, result


def _parse_timestamp(value) -> Optional[int]:
    """Parse timestamp from various formats."""
    if value is None:
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        # Try YYYYMMDD format
        if len(value) == 8 and value.isdigit():
            try:
                dt = datetime.strptime(value, "%Y%m%d")
                return int(dt.timestamp())
            except ValueError:
                pass
        # Try ISO format
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            pass

    return None


async def fetch_single_channel(channel_id: str, site: str, channel_url: str):
    """Fetch and cache videos for a single channel.

    Used when a new subscription is added to immediately populate the feed.
    Runs in the background to avoid blocking the API response.
    """
    try:
        videos, pagination_info, channel_metadata = await fetch_channel_feed(channel_id, site, channel_url)

        if videos:
            database.upsert_cached_videos(channel_id, site, videos)
            database.update_fetch_status(
                channel_id,
                site,
                success=True,
                max_videos_fetched=pagination_info.get("total_fetched") if pagination_info else len(videos),
                pagination_limited=pagination_info.get("pagination_limited", False) if pagination_info else False,
                pagination_limit_reason=pagination_info.get("limit_reason") if pagination_info else None,
            )

            # Save channel metadata if available (subscriber count, verified status)
            if channel_metadata:
                database.update_channel_metadata(
                    channel_id,
                    site,
                    subscriber_count=channel_metadata.get("subscriber_count"),
                    is_verified=channel_metadata.get("is_verified"),
                )


            if pagination_info and pagination_info.get("pagination_limited"):
                logger.warning(
                    f"[Feed] {channel_id} ({site}): Pagination limited - "
                    f"{pagination_info.get('total_fetched')} videos fetched "
                    f"(reason: {pagination_info.get('limit_reason')})"
                )
            logger.info(f"Fetched {len(videos)} videos for new subscription {channel_id} ({site})")
        else:
            logger.warning(f"No videos found for new subscription {channel_id} ({site})")
            database.update_fetch_status(channel_id, site, success=True)

    except _FEED_FETCH_ERRORS as e:
        error_msg = str(e)[:200]
        logger.error(f"Failed to fetch new subscription {channel_id} ({site}): {error_msg}")
        database.update_fetch_status(channel_id, site, success=False, error=error_msg)


async def fetch_all_channels(force: bool = False):
    """Fetch videos for all watched channels."""
    channels = database.get_all_watched_channels()

    if not channels:
        logger.info("No channels to fetch")
        return

    interval = _feed_interval_seconds()
    now = datetime.now(UTC)
    due = channels if force else [ch for ch in channels if not _is_recent_feed(ch.get("last_fetch"), interval, now)]
    skipped = len(channels) - len(due)
    logger.info(
        "[Feed] %d channels due, %d fresh/skipped (%d-minute interval)",
        len(due), skipped, interval // 60,
    )
    if not due:
        return

    success_count = 0
    error_count = 0
    limited_count = 0
    ytdlp_fallback_count = 0

    for channel in due:
        channel_id = channel["channel_id"]
        site = channel["site"]
        channel_url = channel["channel_url"]

        try:
            # Check if this channel had 414 error before (to detect fallback usage)
            had_414_before = False
            with database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT pagination_limit_reason FROM feed_fetch_status WHERE channel_id = ? AND site = ?",
                    (channel_id, site),
                )
                row = cursor.fetchone()
                if row and row[0] == "414_error":
                    had_414_before = True

            videos, pagination_info, channel_metadata = await fetch_channel_feed(channel_id, site, channel_url)

            if videos:
                database.upsert_cached_videos(channel_id, site, videos)
                database.update_fetch_status(
                    channel_id,
                    site,
                    success=True,
                    max_videos_fetched=pagination_info.get("total_fetched") if pagination_info else len(videos),
                    pagination_limited=pagination_info.get("pagination_limited", False) if pagination_info else False,
                    pagination_limit_reason=pagination_info.get("limit_reason") if pagination_info else None,
                )

                # Save channel metadata if available (subscriber count, verified status)
                if channel_metadata:
                    database.update_channel_metadata(
                        channel_id,
                        site,
                        subscriber_count=channel_metadata.get("subscriber_count"),
                        is_verified=channel_metadata.get("is_verified"),
                    )


                # Track if yt-dlp fallback was used (had 414 before, no pagination_info now means fallback used)
                s = get_settings()
                ytdlp_fallback_used = (
                    had_414_before
                    and not pagination_info
                    and s.feed_fallback_ytdlp_on_414
                    and site.lower() == "youtube"
                )
                if ytdlp_fallback_used:
                    ytdlp_fallback_count += 1

                if pagination_info and pagination_info.get("pagination_limited"):
                    limited_count += 1
                    if pagination_info.get("limit_reason") == "414_error":
                        logger.warning(
                            f"[Feed] {channel_id} ({site}): 414 error - limited to "
                            f"{pagination_info.get('total_fetched')} videos"
                        )

                logger.debug(f"Cached {len(videos)} videos for {channel_id} ({site})")
                success_count += 1
            else:
                logger.warning(f"No videos found for {channel_id} ({site})")
                database.update_fetch_status(channel_id, site, success=True)
                success_count += 1

        except _FEED_FETCH_ERRORS as e:
            error_msg = str(e)[:200]  # Truncate long error messages
            logger.error(f"Failed to fetch {channel_id} ({site}): {error_msg}")
            database.update_fetch_status(channel_id, site, success=False, error=error_msg)
            error_count += 1

        # Delay between channel fetches to avoid rate limiting
        s = get_settings()
        await asyncio.sleep(s.feed_channel_delay)

    # Count 414 errors from database for final summary
    limited_414_count = 0
    with database.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM feed_fetch_status WHERE pagination_limit_reason = '414_error'")
        row = cursor.fetchone()
        if row:
            limited_414_count = row[0]

    s = get_settings()
    if ytdlp_fallback_count > 0 and s.feed_fallback_ytdlp_on_414:
        logger.info(
            f"Feed fetch complete: {success_count} succeeded, {error_count} failed, "
            f"{limited_count} pagination-limited this cycle ({limited_414_count} total with 414 errors), "
            f"{ytdlp_fallback_count} used yt-dlp fallback"
        )
    else:
        logger.info(
            f"Feed fetch complete: {success_count} succeeded, {error_count} failed, "
            f"{limited_count} pagination-limited this cycle ({limited_414_count} total with 414 errors)"
        )


async def feed_fetch_loop():
    """Main loop for periodic feed fetching."""
    if config.FEED_STARTUP_DELAY_SECONDS:
        logger.info("[Feed] Delaying startup sweep by %d seconds", config.FEED_STARTUP_DELAY_SECONDS)
        await asyncio.sleep(config.FEED_STARTUP_DELAY_SECONDS)
    while True:
        try:
            await fetch_all_channels()

            # Cleanup old cached videos
            s = get_settings()
            database.cleanup_old_cached_videos(days=s.feed_video_max_age)

            # Cleanup stale watched channels (not requested in 14 days)
            database.cleanup_stale_watched_channels(days=14)

            # Cleanup orphaned cached videos (channels no longer watched or subscribed)
            database.cleanup_orphaned_cached_videos()

        except Exception as e:
            logger.error(f"Feed fetch loop error: {e}", exc_info=True)

        # Wait for next fetch cycle
        await asyncio.sleep(_feed_interval_seconds())


def start_feed_fetcher():
    """Start the background feed fetcher task."""
    global _fetch_task
    if _fetch_task is None or _fetch_task.done():
        _fetch_task = asyncio.create_task(feed_fetch_loop())
        logger.info("Started feed fetcher (interval: %ds)", _feed_interval_seconds())


def stop_feed_fetcher():
    """Stop the background feed fetcher task."""
    global _fetch_task
    if _fetch_task and not _fetch_task.done():
        _fetch_task.cancel()
        logger.info("Stopped feed fetcher")
