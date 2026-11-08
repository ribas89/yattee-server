"""Video format availability policy for clients using Invidious-style responses."""

import logging

import config
from models import VideoResponse

logger = logging.getLogger(__name__)


def prefer_combined_streams(response: VideoResponse) -> VideoResponse:
    """Prioritize playable progressive video+audio over DASH split tracks.

    Yattee clients pick their own formats from the Invidious-style response.
    Ordering the streams alone does not stop a client from picking itag 134
    at 360p instead of muxed itag 18. When a combined stream is available,
    hide only adaptive *video* tracks, so playback uses one media stream.
    Keep adaptive audio-only tracks for explicit audio-only mode.

    Do not treat HLS manifests as progressive media; leave adaptive tracks
    available if HLS is the only item in formatStreams.
    """
    if not config.PREFER_COMBINED_STREAMS or response.liveNow:
        return response
    combined = [
        stream for stream in response.formatStreams
        if stream.url and (stream.type or "").lower().startswith("video/")
        and (stream.container or "").lower() not in {"hls", "m3u8"}
    ]
    if not combined:
        return response

    previous = len(response.adaptiveFormats)
    response.adaptiveFormats = [
        stream for stream in response.adaptiveFormats
        if not (stream.type or "").lower().startswith("video/")
    ]
    removed = previous - len(response.adaptiveFormats)
    # Put itag 18 first when present; preserve the order of other formats.
    response.formatStreams.sort(key=lambda stream: stream.itag != "18")
    if removed:
        logger.info(
            "[Videos] Prefer combined for %s: %s muxed streams, "
            "suppressed %s adaptive video tracks (audio-only retained)",
            response.videoId, len(combined), removed,
        )
    return response


def add_combined_candidate(response: VideoResponse, candidate: VideoResponse) -> VideoResponse:
    """Restore a usable muxed candidate discarded by the InnerTube/yt-dlp join.

    InnerTube sometimes publishes only DASH format shapes, even when yt-dlp
    extracted progressive itag 18. Use the same already-extracted yt-dlp
    response as fallback, without triggering another YouTube request.
    """
    if not config.PREFER_COMBINED_STREAMS or response.liveNow:
        return response
    existing = {fmt.itag for fmt in response.formatStreams}
    # Progressive 360p is the observed reliable fallback. If no muxed stream
    # exists at all, permit another muxed format as a last resort.
    progressive = [
        f for f in candidate.formatStreams
        if f.url and (f.type or "").lower().startswith("video/")
        and (f.container or "").lower() not in {"hls", "m3u8"}
    ]
    preferred = next((f for f in progressive if f.itag == "18" and f.itag not in existing), None)
    if preferred:
        response.formatStreams.append(preferred)
        logger.info("[Videos] Recovered yt-dlp progressive itag 18 for %s", response.videoId)
    elif not any(
        f.url and (f.type or "").lower().startswith("video/")
        and (f.container or "").lower() not in {"hls", "m3u8"}
        for f in response.formatStreams
    ):
        alt = next((f for f in progressive if f.itag not in existing), None)
        if alt:
            response.formatStreams.append(alt)
            logger.info("[Videos] Recovered yt-dlp combined format %s for %s", alt.itag, response.videoId)
    return response
