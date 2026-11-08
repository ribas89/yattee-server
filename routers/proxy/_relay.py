"""Byte-relay proxy endpoint for playback.

Unlike ``/proxy/fast/{video_id}``, this endpoint takes the upstream URL
itself (HMAC-signed by the server at extraction time) and streams it
through to the client without mandatory yt-dlp re-extraction. Cached byte ranges can be replayed from disk.

Why a separate endpoint:

- ``/proxy/fast/`` re-runs yt-dlp on every request, which (a) re-downloads
  the whole file to ``/downloads/{id}_{itag}.{ext}`` before streaming and
  (b) fails outright when yt-dlp can't extract the video (e.g. ended live
  streams: ``This live event has ended``). For those, Invidious or
  InnerTube may have already returned a perfectly good URL, but the fast
  endpoint throws it away.

- The relay just streams bytes from upstream → client with full Range
  passthrough. Time-to-first-byte is small, seek works, and any URL the
  converter can produce (googlevideo, Invidious /videoplayback, an HLS
  variant manifest) is supported uniformly.

For HLS/DASH manifests, the relay rewrites segment URLs in the body so
segments also flow back through the relay (otherwise individual segments
would still hit googlevideo directly and could 403 for the same
client-IP-mismatch reason that motivated all of this).

``/proxy/fast/`` stays in place — it's still the right shape for
**downloads**, where caching the file on disk is desirable.
"""

import base64
import hashlib
import hmac
import logging
import base64 as _b64
import binascii
import json
import random
import re
import time
import urllib.parse
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

import config
import tokens as token_utils
import video_cache
from egress import is_youtube_url, local_address_for
from routers.proxy._streaming import router
from settings import get_settings
from ytdlp_wrapper import is_safe_url

logger = logging.getLogger(__name__)


DEFAULT_TTL_SECONDS = 6 * 60 * 60

# The upstream headers are non-sensitive headers supplied by yt-dlp's format.
# They are embedded in the signed URL; cookies and authorization are excluded.
_DOWNLOAD_HEADER_NAMES = {
    "user-agent": "User-Agent",
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "referer": "Referer",
    "origin": "Origin",
    "sec-fetch-mode": "Sec-Fetch-Mode",
}


def _media_cache_type(url: str, claimed_type: str) -> str:
    """Determine the real media MIME for signed GoogleVideo playback URLs.

    GoogleVideo may report application/octet-stream for byte-range probes.
    In that case its already-HMAC-verified `mime` URL parameter is the useful
    media type. Do not trust arbitrary hosts or non-videoplayback paths.
    """
    normalized = claimed_type.split(";", 1)[0].strip().lower()
    if normalized.startswith(("video/", "audio/")):
        return normalized
    if not is_youtube_url(url):
        return normalized
    parsed = urllib.parse.urlsplit(url)
    if not parsed.path.endswith("/videoplayback"):
        return normalized
    embedded = urllib.parse.parse_qs(parsed.query).get("mime", [""])[0]
    embedded = embedded.split(";", 1)[0].strip().lower()
    if embedded.startswith(("video/", "audio/")):
        return embedded
    return normalized


def _safe_download_headers(headers: Optional[dict]) -> dict[str, str]:
    result = {}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        canonical = _DOWNLOAD_HEADER_NAMES.get(name.lower())
        if canonical and len(value) <= 400 and not any(c in value for c in "\r\n\x00"):
            result[canonical] = value
    return result


def _encode_download_headers(headers: Optional[dict]) -> Optional[str]:
    selected = _safe_download_headers(headers)
    if not selected:
        return None
    payload = json.dumps(selected, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_download_headers(value: Optional[str]) -> dict[str, str]:
    if not value:
        return {}
    if len(value) > 2000 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid signed upstream headers")
    try:
        payload = _b64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parsed = json.loads(payload)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid signed upstream headers") from exc
    if not isinstance(parsed, dict) or len(parsed) > len(_DOWNLOAD_HEADER_NAMES):
        raise ValueError("Invalid signed upstream headers")
    return _safe_download_headers(parsed)

# Upstream video range size is configured through HTTP_CHUNK_SIZE.
# Defaults to 1 MiB; recreate the container after changing its environment.
CHUNK_SIZE = config.HTTP_CHUNK_SIZE

# How many times we retry a single chunk before giving up. The retry uses the
# byte offset we've already yielded to the client, so partial progress isn't
# lost — we just resume the upstream Range from where we stopped.
MAX_RETRIES_PER_CHUNK = 6
MIN_CHUNK_SIZE = 64 * 1024

# Errors that justify a chunk retry. These are typically a TCP RST mid-read
# (googlevideo recycling connections) or a ProtocolError from h2/keep-alive.
# Connect-time errors are also retryable since the new chunk opens a fresh
# connection. We don't retry HTTPStatusError (4xx/5xx) — that's authoritative.
RETRYABLE_UPSTREAM_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.ReadTimeout,
)

HLS_CONTENT_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl")
DASH_CONTENT_TYPES = ("application/dash+xml",)

# yt-dlp's default UA — what googlevideo expects.
UPSTREAM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Headers we accept from the client and forward to upstream.
_FORWARDED_REQUEST_HEADERS = ("range", "if-range", "if-none-match", "if-modified-since")

# Headers we copy from upstream back to the client.
_PASSTHROUGH_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
    "cache-control",
    "expires",
)


def _sign(url: str, exp: int, video_id: Optional[str] = None, itag: Optional[str] = None, rh: Optional[str] = None) -> str:
    """HMAC-SHA256 of ``url:exp`` keyed by the server's stream-token secret.

    Same key the existing token system uses (``tokens._get_signing_key``),
    so no new secret to provision.
    """
    payload_text = f"{url}:{exp}"
    if video_id is not None and itag is not None:
        payload_text += f":video={video_id}:itag={itag}"
    if rh is not None:
        payload_text += f":rh={rh}"
    payload = payload_text.encode("utf-8")
    digest = hmac.new(token_utils._get_signing_key(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")


def _verify(
    url: str, exp: int, sig: str,
    video_id: Optional[str] = None, itag: Optional[str] = None, rh: Optional[str] = None,
) -> bool:
    expected = _sign(url, exp, video_id, itag, rh)
    return hmac.compare_digest(expected, sig)


def signed_relay_url(
    base_url: str,
    upstream_url: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    content_type: Optional[str] = None,
    video_id: Optional[str] = None,
    itag: Optional[str] = None,
    upstream_headers: Optional[dict] = None,
) -> str:
    """Mint a ``/proxy/relay?...`` URL the client can hit to play this stream.

    The signature gates "only the converter could have produced this URL".
    Without it the relay would be an open HTTP proxy.
    """
    exp = int(time.time()) + ttl_seconds
    # Bind refresh metadata to the existing HMAC. Ordinary signed URLs and
    # manifest/segment links keep their original three-parameter format.
    if (video_id is None) != (itag is None):
        raise ValueError("video_id and itag must be supplied together")
    rh = _encode_download_headers(upstream_headers)
    sig = _sign(upstream_url, exp, video_id, itag, rh)
    params = {"url": upstream_url, "sig": sig, "exp": str(exp)}
    if rh:
        params["rh"] = rh
    if video_id is not None:
        params["video_id"] = video_id
        params["itag"] = itag
    if content_type:
        params["ct"] = content_type
    return f"{base_url.rstrip('/')}/proxy/relay?{urllib.parse.urlencode(params)}"


def signed_cached_url(base_url: str, video_id: str, itag: str, ttl_seconds: int = 86400) -> str:
    """Mint an authenticated local-only URL for a fully cached format.

    Unlike an old upstream URL, this works after YouTube media signatures expire.
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id) or not re.fullmatch(
        r"[0-9]{1,5}(?:-[A-Za-z0-9_]{1,32}){0,3}", itag
    ):
        raise ValueError("Invalid cache key")
    exp = int(time.time()) + ttl_seconds
    key = f"cache://{video_id}/{itag}"
    signature = _sign(key, exp)
    path = f"/proxy/cached/{video_id}/{itag}"
    return f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode({'exp': exp, 'sig': signature})}"


@router.get("/cached/{video_id}/{itag}")
async def serve_cached_media(video_id: str, itag: str, request: Request, exp: int, sig: str):
    """Serve completed video bytes without any dependency on YouTube metadata."""
    try:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id) or not re.fullmatch(
            r"[0-9]{1,5}(?:-[A-Za-z0-9_]{1,32}){0,3}", itag
        ):
            raise HTTPException(status_code=403, detail="Invalid cache key")
        if time.time() > exp or not _verify(f"cache://{video_id}/{itag}", exp, sig):
            raise HTTPException(status_code=403, detail="Invalid cache token")
        cached = video_cache.lookup(video_id, itag)
        if cached is None:
            raise HTTPException(status_code=404, detail="Completed video not cached")
        logger.info("[VideoCache] HIT-LOCAL %s:%s", video_id, itag)
        return video_cache.cached_response(cached, request.headers.get("range"))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid cache token") from exc


# --- HLS manifest rewriting -------------------------------------------------

# Lines that are URLs in HLS manifests: either bare lines (segment) or the
# URI="..." attribute on EXT-X-KEY / EXT-X-MAP / EXT-X-MEDIA tags.
_HLS_URI_ATTR_RE = re.compile(r'(URI=")([^"]+)(")')


def _rewrite_hls(body: str, base_url: str, manifest_url: str, ttl_seconds: int) -> str:
    """Rewrite every segment / sub-playlist URL in an HLS manifest to a
    fresh signed relay URL.

    `manifest_url` is needed to resolve relative segment paths.
    """

    def absolutize(target: str) -> str:
        return urllib.parse.urljoin(manifest_url, target)

    out_lines = []
    for line in body.splitlines():
        stripped = line.strip()

        # Lines that aren't URLs we just keep as-is...
        if not stripped or stripped.startswith("#"):
            # ...except for tag lines that embed a URI="..." attribute.
            if "URI=" in stripped:
                def _sub(match):
                    signed = signed_relay_url(base_url, absolutize(match.group(2)), ttl_seconds=ttl_seconds)
                    return f"{match.group(1)}{signed}{match.group(3)}"
                line = _HLS_URI_ATTR_RE.sub(_sub, line)
            out_lines.append(line)
            continue

        # Bare URL line (segment or variant playlist)
        out_lines.append(signed_relay_url(base_url, absolutize(stripped), ttl_seconds=ttl_seconds))

    return "\n".join(out_lines) + ("\n" if body.endswith("\n") else "")


# --- DASH manifest rewriting ------------------------------------------------

# DASH manifests use BaseURL elements + SegmentTemplate/SegmentList with
# media="..." / initialization="...". A correct rewrite requires real XML
# parsing and resolution of segment templates. For v1 we leave DASH bodies
# untouched — the iOS client already prefers HLS first, and yt-dlp's DASH
# manifests use absolute googlevideo URLs whose `ip=` may still be bound,
# so this is a known limitation. Logged below so we notice it.

# --- Range / chunked upstream helpers --------------------------------------

_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")


def _parse_client_range(header: Optional[str]) -> tuple[int, Optional[int]]:
    """Parse a single-range ``Range: bytes=start-end`` (end may be empty).

    Returns ``(start, end_or_None)``. Anything we can't parse falls back to
    ``(0, None)`` — i.e. "stream from the beginning, end at content end".
    Multi-range and suffix-range forms aren't supported (MPV doesn't use
    them; Range parsing for them is more bookkeeping than it's worth).
    """
    if not header:
        return 0, None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return 0, None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    return start, end


def _parse_content_range_total(header: Optional[str]) -> Optional[int]:
    """Pull the ``/TOTAL`` from ``Content-Range: bytes X-Y/TOTAL``.

    Returns ``None`` if the header is missing or upstream sent ``*`` for the
    total (indicating it doesn't know the full size — happens for chunked
    responses).
    """
    if not header:
        return None
    m = _CONTENT_RANGE_RE.match(header.strip())
    if not m or m.group(3) == "*":
        return None
    return int(m.group(3))


async def _stream_chunked_with_retry(
    client: httpx.AsyncClient,
    url: str,
    base_headers: dict,
    start: int,
    end_inclusive: Optional[int],
    refresh_url: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
):
    """Stream bytes ``start..end_inclusive`` (HTTP-style inclusive end) from
    upstream, breaking the read into ``CHUNK_SIZE``-sized upstream GETs.

    Per chunk, retry up to ``MAX_RETRIES_PER_CHUNK`` times on transient
    connection errors. The retry resumes from the byte offset already
    yielded, so the client sees a contiguous stream regardless of upstream
    flakiness — bytes already delivered are never delivered twice.

    If ``end_inclusive`` is ``None`` we keep going until upstream returns
    fewer bytes than requested (signalling end-of-content) or returns a
    non-success status.
    """
    cursor = start
    active_chunk_size = CHUNK_SIZE
    refreshes_remaining = 3

    while end_inclusive is None or cursor <= end_inclusive:
        # Match yt-dlp's HTTP downloader: vary each chunk between 95% and
        # 100% of the configured maximum, rather than fixed MiB boundaries.
        request_size = random.randint(max(1, int(active_chunk_size * 0.95)), active_chunk_size)
        chunk_end = cursor + request_size - 1
        if end_inclusive is not None:
            chunk_end = min(chunk_end, end_inclusive)

        chunk_yielded = 0
        last_error: Optional[BaseException] = None
        refreshed_this_chunk = False

        for attempt in range(MAX_RETRIES_PER_CHUNK):
            attempt_start = cursor + chunk_yielded
            req_headers = {**base_headers, "Range": f"bytes={attempt_start}-{chunk_end}"}

            try:
                resp = await client.send(
                    client.build_request("GET", url, headers=req_headers),
                    stream=True,
                )
            except RETRYABLE_UPSTREAM_ERRORS as e:
                last_error = e
                logger.warning(
                    f"[Relay] connect retry {attempt + 1}/{MAX_RETRIES_PER_CHUNK} "
                    f"for bytes={attempt_start}-{chunk_end}: {e}"
                )
                continue

            # 416 Range Not Satisfiable when end_inclusive was unknown means
            # we walked past content end — clean exit.
            if resp.status_code == 416 and end_inclusive is None:
                await resp.aclose()
                return

            if (resp.status_code == 403 and refresh_url is not None
                    and refreshes_remaining > 0 and not refreshed_this_chunk):
                # Retry with the same media format and unchanged byte offset.
                await resp.aclose()
                refreshed_this_chunk = True
                refreshes_remaining -= 1
                try:
                    replacement = await refresh_url(url)
                except Exception as exc:
                    logger.warning("[Relay] Could not refresh upstream media URL: %s", type(exc).__name__)
                    replacement = None
                if replacement and replacement != url:
                    logger.warning("[Relay] Re-extracted media URL after 403 at offset %d", attempt_start)
                    url = replacement
                    continue
                # Refresh failed; fall through to the smaller-range retry.
                # Keep the original status available without sending an
                # unnecessary duplicate request.

            if resp.status_code == 403 and chunk_end - attempt_start + 1 > MIN_CHUNK_SIZE:
                # Reduce range length when upstream rejects the request.
                previous_size = chunk_end - attempt_start + 1
                active_chunk_size = max(MIN_CHUNK_SIZE, previous_size // 2)
                chunk_end = attempt_start + active_chunk_size - 1
                if end_inclusive is not None:
                    chunk_end = min(chunk_end, end_inclusive)
                await resp.aclose()
                logger.warning(
                    "[Relay] 403 for %d-byte range; retrying with %d bytes at offset %d",
                    previous_size, chunk_end - attempt_start + 1, attempt_start,
                )
                continue

            if resp.status_code >= 400:
                # Do not log the original media URL (it contains signed tokens).
                status = resp.status_code
                await resp.aclose()
                raise httpx.HTTPStatusError(
                    f"upstream {status} for bytes={attempt_start}-{chunk_end}",
                    request=resp.request,
                    response=resp,
                )

            # If we asked for a Range and upstream answered 200, it doesn't
            # honour Range — we'd be re-downloading the whole body from byte 0
            # for every chunk. Bail to single-shot mode by streaming this one
            # body fully and stopping. (googlevideo always 206s; this only
            # matters as a safety net for misconfigured upstreams.)
            single_shot = resp.status_code == 200
            try:
                async for piece in resp.aiter_raw():
                    chunk_yielded += len(piece)
                    yield piece
                await resp.aclose()
                break
            except RETRYABLE_UPSTREAM_ERRORS as e:
                last_error = e
                logger.warning(
                    f"[Relay] read retry {attempt + 1}/{MAX_RETRIES_PER_CHUNK} "
                    f"after {chunk_yielded}B of bytes={cursor}-{chunk_end}: {e}"
                )
                await resp.aclose()
                continue
        else:
            # Exhausted retries.
            raise last_error if last_error else RuntimeError("relay chunk failed")

        # A zero-byte successful response is not EOF when we KNOW that the
        # downstream client expects more bytes; fail visibly instead of
        # completing a truncated Content-Length response.
        if chunk_yielded == 0:
            if end_inclusive is not None:
                raise RuntimeError(
                    f"upstream returned an empty body for bytes={cursor}-{chunk_end}"
                )
            return

        chunk_request_size = chunk_end - cursor + 1
        cursor += chunk_yielded

        # Single-shot mode (upstream ignored Range) — we just streamed the
        # whole body, anything more would be duplicates.
        if single_shot:
            return

        # Some upstreams return a smaller 206 range than we requested. That
        # is NOT EOF while the downstream response still has known remaining
        # bytes. Request the next chunk from the exact offset received, so
        # no bytes are repeated or skipped.
        if chunk_yielded < chunk_request_size:
            if end_inclusive is None or cursor > end_inclusive:
                return
            logger.warning(
                "[Relay] short upstream range (%d/%d bytes); continuing at byte %d",
                chunk_yielded, chunk_request_size, cursor,
            )


# --- Endpoint ---------------------------------------------------------------


@router.get("/relay")
async def relay(
    request: Request,
    url: str,
    sig: str,
    exp: int,
    ct: Optional[str] = None,
    video_id: Optional[str] = None,
    itag: Optional[str] = None,
    rh: Optional[str] = None,
):
    if int(time.time()) > exp:
        raise HTTPException(status_code=403, detail="Relay URL expired")

    if (video_id is None) != (itag is None):
        raise HTTPException(status_code=403, detail="Invalid refresh metadata")
    if video_id is not None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id) or not (itag and len(itag) <= 64 and re.fullmatch(r"[0-9]{1,5}(?:-[A-Za-z0-9_]{1,32}){0,3}", itag)):
            raise HTTPException(status_code=403, detail="Invalid refresh metadata")
    if not _verify(url, exp, sig, video_id, itag, rh):
        raise HTTPException(status_code=403, detail="Invalid relay signature")
    try:
        signed_headers = _decode_download_headers(rh)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid upstream headers") from exc

    # SSRF guard. The signature already prevents arbitrary URLs from
    # reaching here, but if the converter ever produced a URL pointing at
    # an internal service we still want to refuse.
    if not is_safe_url(url):
        raise HTTPException(status_code=403, detail="URL targets restricted network resources")

    # A completed format is keyed by (video_id, itag), independent of the
    # ephemeral upstream signature. Check BEFORE contacting YouTube.
    if video_id is not None and itag is not None and is_youtube_url(url):
        cached = video_cache.lookup(video_id, itag)
        if cached is not None:
            logger.info("[VideoCache] HIT %s:%s", video_id, itag)
            return video_cache.cached_response(cached, request.headers.get("range"))
        # Intermittent seeking need not fetch bytes we have already saved.
        partial_response = video_cache.cached_partial_response(
            video_id, itag, request.headers.get("range"),
        )
        if partial_response is not None:
            logger.info("[VideoCache] PARTIAL-HIT %s:%s", video_id, itag)
            return partial_response

    upstream_headers = {
        "User-Agent": UPSTREAM_USER_AGENT,
        # Don't let httpx negotiate gzip/br upstream — we relay raw bytes and
        # would have to either pass Content-Encoding through or decompress.
        # For media this is overwhelmingly the right call: video bytes are
        # not re-compressible anyway, and avoiding it sidesteps a class of
        # passthrough bugs.
        "Accept-Encoding": "identity",
        **signed_headers,
    }
    # Forward client conditionals; the client's Range is parsed/applied
    # below per-chunk, so don't pass it raw to the meta probe.
    for h in _FORWARDED_REQUEST_HEADERS:
        if h == "range":
            continue
        v = request.headers.get(h)
        if v is not None:
            upstream_headers[h] = v

    client_range = request.headers.get("range")
    range_start, range_end = _parse_client_range(client_range)

    # For YouTube-family hosts, honor the egress proxy / forced IP family.
    # Other upstreams (LAN Invidious, generic sites) keep the plain client —
    # an explicit transport would also disable HTTP(S)_PROXY env mounts.
    # proxy and local_addr are never both set: effective_ip_family() returns
    # "auto" while the proxy is active (httpx drops local_address otherwise).
    transport = None
    if is_youtube_url(url):
        s = get_settings()
        proxy = s.effective_yt_egress_proxy()
        local_addr = local_address_for(s.effective_ip_family())
        if proxy or local_addr:
            transport = httpx.AsyncHTTPTransport(proxy=proxy, local_address=local_addr)

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
        follow_redirects=True,
        **({"transport": transport} if transport else {}),
    )

    # URL refresh is limited to signed yt-dlp-derived YouTube streams.
    refresh_enabled = bool(video_id and itag and is_youtube_url(url))

    # --- Meta probe: tiny first request to discover total/status/type ----
    # We use Range: bytes=0-0 to get just the first byte (1 B body) so we
    # can read Content-Range/total without downloading anything significant.
    # The actual payload is fetched fresh via _stream_chunked_with_retry
    # below; this connection is closed before streaming starts.
    meta_headers = {**upstream_headers, "Range": "bytes=0-0"}
    try:
        meta_req = client.build_request("GET", url, headers=meta_headers)
        meta = await client.send(meta_req, stream=True)
    except httpx.RequestError as e:
        await client.aclose()
        logger.warning(f"[Relay] Upstream connect failed for {url[:120]}: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream connect failed: {e}") from e

    if meta.status_code >= 400:
        status = meta.status_code
        await meta.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream metadata request returned {status}")

    upstream_content_type = meta.headers.get("content-type", "")
    response_content_type = _media_cache_type(url, ct or upstream_content_type or "")
    # If GoogleVideo's metadata probe was generic, correct the response MIME
    # using the signed URL so downstream clients and the cache agree.
    inferred_media_type = (
        response_content_type.startswith(("video/", "audio/"))
        and not (ct or upstream_content_type or "").split(";", 1)[0].strip().lower().startswith(("video/", "audio/"))
    )
    is_hls = response_content_type in HLS_CONTENT_TYPES or url.split("?", 1)[0].endswith(".m3u8")
    is_dash = response_content_type in DASH_CONTENT_TYPES or url.split("?", 1)[0].endswith(".mpd")

    # --- Manifest path: buffer + rewrite + return one-shot --------------
    # HLS manifests are tiny — just refetch in full (the meta probe only got
    # 1 byte), then rewrite segment URLs. Don't chunk; don't retry; don't
    # try to be clever.
    if is_hls and meta.status_code in (200, 206):
        await meta.aclose()
        try:
            full = await client.get(url, headers={**upstream_headers})
            text = full.text
            base_url_self = f"{request.url.scheme}://{request.url.netloc}"
            rewritten = _rewrite_hls(text, base_url=base_url_self, manifest_url=url, ttl_seconds=DEFAULT_TTL_SECONDS)
            headers = {"content-type": "application/vnd.apple.mpegurl"}
            for h in ("cache-control", "etag", "last-modified"):
                v = full.headers.get(h)
                if v:
                    headers[h] = v
            return StreamingResponse(
                iter([rewritten.encode("utf-8")]),
                status_code=full.status_code,
                headers=headers,
            )
        finally:
            await client.aclose()

    if is_dash:
        # Known limitation — pass through with a warning. See note above.
        logger.info(f"[Relay] DASH manifest passthrough (segments not rewritten): {url[:120]}")

    # --- Binary path: chunked stream-and-relay ---------------------------
    total = _parse_content_range_total(meta.headers.get("content-range"))
    if total is None:
        # Fallback: maybe upstream sent Content-Length on a 200 (no Range
        # support). Use that.
        cl = meta.headers.get("content-length")
        if cl and meta.status_code == 200:
            try:
                total = int(cl)
            except ValueError:
                pass

    async def refresh_media_url(previous_url: str) -> Optional[str]:
        # Never switch formats or change file size mid-play.
        from ytdlp_wrapper._youtube import get_video_info

        info = await get_video_info(video_id, use_cache=False)
        format_match = next(
            (fmt for fmt in info.get("formats", [])
             if str(fmt.get("format_id")) == itag and fmt.get("url")),
            None,
        )
        if not format_match:
            return None
        candidate = format_match["url"]
        replacement_headers = {**upstream_headers, **_safe_download_headers(format_match.get("http_headers"))}
        if candidate == previous_url or not is_youtube_url(candidate) or not is_safe_url(candidate):
            return None
        old_size = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("clen", [None])[0]
        new_size = urllib.parse.parse_qs(urllib.parse.urlsplit(candidate).query).get("clen", [None])[0]
        if old_size and new_size and old_size != new_size:
            logger.warning("[Relay] Refusing refreshed URL: media byte length changed")
            return None
        try:
            probe = await client.get(
                candidate, headers={**replacement_headers, "Range": "bytes=0-0"}
            )
        except httpx.RequestError:
            return None
        if probe.status_code != 206:
            return None
        candidate_total = _parse_content_range_total(probe.headers.get("content-range"))
        if total is not None and candidate_total != total:
            logger.warning("[Relay] Refusing refreshed URL: Content-Range total changed")
            return None
        upstream_headers.update(replacement_headers)
        return candidate

    # Validate seek ranges before writing any HTTP response headers.
    # Clients can send open-ended and suffix ranges while fast-forwarding.
    if client_range and total is not None:
        raw_range = client_range.strip()
        suffix = re.fullmatch(r"bytes=-(\d+)", raw_range)
        standard = re.fullmatch(r"bytes=(\d+)-(\d*)", raw_range)
        if suffix and int(suffix.group(1)) > 0:
            range_start = max(0, total - int(suffix.group(1)))
            range_end = total - 1
        elif standard:
            range_start = int(standard.group(1))
            range_end = int(standard.group(2)) if standard.group(2) else None
        else:
            await meta.aclose()
            await client.aclose()
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        if range_start >= total or (range_end is not None and range_end < range_start):
            await meta.aclose()
            await client.aclose()
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})

    # Return standard range metadata to the player.
    if client_range and total is not None:
        loop_end = range_end if range_end is not None else (total - 1)
        # Clamp range_end to content end if client asked for more than exists.
        loop_end = min(loop_end, total - 1)
        response_status = 206
        response_headers = {
            "content-range": f"bytes {range_start}-{loop_end}/{total}",
            "content-length": str(loop_end - range_start + 1),
            "accept-ranges": "bytes",
        }
    elif total is not None:
        loop_end = total - 1
        response_status = 200
        response_headers = {
            "content-length": str(total),
            "accept-ranges": "bytes",
        }
    else:
        # Total unknown (chunked upstream, or the meta probe failed to give
        # us one). Stream open-ended; let _stream_chunked_with_retry stop
        # when upstream signals EOF via 416 / short read.
        loop_end = range_end
        response_status = 206 if client_range else (meta.status_code or 200)
        response_headers = {"accept-ranges": "bytes"}

    # Copy over a few content-classification headers from upstream.
    for h in ("content-type", "etag", "last-modified", "cache-control", "expires"):
        v = meta.headers.get(h)
        if v:
            response_headers[h] = v
    if ct:
        response_headers["content-type"] = ct
    if inferred_media_type:
        response_headers["content-type"] = response_content_type

    await meta.aclose()

    if video_id and itag and is_youtube_url(url):
        if config.VIDEO_CACHE_DAYS == 0:
            logger.info("[VideoCache] BYPASS %s:%s reason=disabled", video_id, itag)
        elif total is None or total <= 0:
            logger.info("[VideoCache] BYPASS %s:%s reason=unknown-size status=%s", video_id, itag, meta.status_code)
        elif meta.status_code != 206:
            logger.info("[VideoCache] BYPASS %s:%s reason=upstream-status-%d", video_id, itag, meta.status_code)
        elif not response_content_type.startswith(("video/", "audio/")):
            logger.info("[VideoCache] BYPASS %s:%s reason=non-media-type mime=%s", video_id, itag, response_content_type)
        else:
            logger.info("[VideoCache] LOOKUP/MISS %s:%s requested=%s-%s size=%d mime=%s",
                        video_id, itag, range_start, loop_end, total, response_content_type)

    # Sparse replay: stitch verified cached spans with precisely the missing
    # byte ranges. No sparse-file holes are ever sent to the client, and each
    # successfully fetched missing span is persisted even if playback stops.
    partial = None
    segments = None
    if (video_id is not None and itag is not None and is_youtube_url(url)
            and total is not None and meta.status_code == 206
            and response_content_type.startswith(("video/", "audio/"))):
        partial = video_cache.lookup_partial(video_id, itag, expected_size=total)
        if partial is not None:
            segments = video_cache.split_ranges(partial, range_start, loop_end + 1)

    if segments is not None and any(cached for cached, _, _ in segments):
        cached_bytes = sum(hi - lo for cached, lo, hi in segments if cached)
        missing_bytes = sum(hi - lo for cached, lo, hi in segments if not cached)
        logger.info("[VideoCache] PARTIAL %s:%s cached=%d upstream-needed=%d requested=%s-%s",
                    video_id, itag, cached_bytes, missing_bytes, range_start, loop_end)
        async def sparse_body():
            for is_cached, lo, hi in segments:
                if is_cached:
                    for piece in video_cache.read_cached_chunks(partial, lo, hi):
                        yield piece
                else:
                    writer = video_cache.begin(
                        video_id, itag, total, response_content_type, lo,
                    )
                    missing_iter = _stream_chunked_with_retry(
                        client=client, url=url, base_headers=upstream_headers,
                        start=lo, end_inclusive=hi - 1,
                        refresh_url=refresh_media_url if refresh_enabled else None,
                    )
                    try:
                        async for piece in missing_iter:
                            if writer is not None:
                                try:
                                    writer.write(piece)
                                except (OSError, ValueError) as exc:
                                    logger.warning("[VideoCache] Partial write failed: %s", exc)
                                    writer.close()
                                    writer = None
                            yield piece
                    finally:
                        if writer is not None:
                            writer.close()
                        await missing_iter.aclose()

        upstream_iter = sparse_body()
        prebuffer = bytearray()
        initial_target = min(CHUNK_SIZE, loop_end - range_start + 1)
        try:
            while len(prebuffer) < initial_target:
                piece = await anext(upstream_iter, None)
                if piece is None:
                    break
                prebuffer.extend(piece)
        except (httpx.HTTPStatusError, *RETRYABLE_UPSTREAM_ERRORS, RuntimeError, OSError) as e:
            logger.warning("[Relay] sparse playback failed before streaming began: %s", e)
            await upstream_iter.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail="Upstream media request failed") from e
        if len(prebuffer) < initial_target:
            await upstream_iter.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail="Upstream returned incomplete initial video range")

        async def sparse_response_body():
            try:
                if prebuffer:
                    yield bytes(prebuffer)
                async for piece in upstream_iter:
                    yield piece
            finally:
                await upstream_iter.aclose()
                await client.aclose()

        response_headers["X-Yattee-Video-Cache"] = "PARTIAL" if any(not c for c, _, _ in segments) else "PARTIAL-HIT"
        return StreamingResponse(
            sparse_response_body(), status_code=response_status,
            headers=response_headers,
        )

    # Prime the first upstream response BEFORE sending 200/206 + a fixed
    # Content-Length to the player. If YouTube rejects the first real range,
    # the client gets a genuine 502 instead of an empty 206 response that
    # Uvicorn later reports as "shorter than Content-Length".
    upstream_iter = _stream_chunked_with_retry(
        client=client,
        url=url,
        base_headers=upstream_headers,
        start=range_start,
        end_inclusive=loop_end,
        refresh_url=refresh_media_url if refresh_enabled else None,
    )
    # Fully prebuffer the first range before committing Content-Length.
    # A failing seek now returns 502 (so the player can retry), rather than
    # a misleading successful 206 followed by an ASGI protocol exception.
    prebuffer = bytearray()
    initial_target = min(CHUNK_SIZE, loop_end - range_start + 1) if loop_end is not None else CHUNK_SIZE
    try:
        while len(prebuffer) < initial_target:
            piece = await anext(upstream_iter, None)
            if piece is None:
                break
            prebuffer.extend(piece)
    except (httpx.HTTPStatusError, *RETRYABLE_UPSTREAM_ERRORS, RuntimeError) as e:
        logger.warning("[Relay] upstream failed before streaming began: %s", e)
        await upstream_iter.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Upstream media request failed") from e

    if len(prebuffer) < initial_target:
        await upstream_iter.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Upstream returned incomplete initial video range")

    # Record byte ranges as the client receives them. Only a fully covered,
    # size-validated media file is published for later playback. The writer
    # survives seeks by merging ranges from successive relay requests.
    cache_writer = None
    if (video_id is not None and itag is not None and is_youtube_url(url)
            and total is not None and meta.status_code == 206
            and response_content_type.startswith(("video/", "audio/"))):
        try:
            cache_writer = video_cache.begin(
                video_id, itag, total, response_content_type, range_start
            )
        except OSError as exc:
            logger.warning("[VideoCache] Unable to open media cache: %s", exc)

    async def body_iter():
        nonlocal cache_writer
        try:
            if prebuffer:
                payload = bytes(prebuffer)
                if cache_writer is not None:
                    try:
                        cache_writer.write(payload)
                    except (OSError, ValueError) as exc:
                        logger.warning("[VideoCache] Cache write failed, playback continues: %s", exc)
                        cache_writer.close()
                        cache_writer = None
                yield payload
            async for piece in upstream_iter:
                if cache_writer is not None:
                    try:
                        cache_writer.write(piece)
                    except (OSError, ValueError) as exc:
                        logger.warning("[VideoCache] Cache write failed, playback continues: %s", exc)
                        cache_writer.close()
                        cache_writer = None
                yield piece
        except Exception as e:
            # Once response headers have been sent, we cannot replace 206
            # with an error response. Don't silently swallow an upstream
            # failure and make a truncated response look successful.
            logger.warning("[Relay] upstream stream ended with an error: %s", e)
            raise
        finally:
            if cache_writer is not None:
                cache_writer.close()
            await upstream_iter.aclose()
            await client.aclose()

    if cache_writer is not None:
        response_headers["X-Yattee-Video-Cache"] = "MISS"
    return StreamingResponse(
        body_iter(),
        status_code=response_status,
        headers=response_headers,
    )
