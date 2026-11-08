# Yattee Server

[![Build Status](https://github.com/yattee/yattee-server/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/yattee/yattee-server/actions/workflows/docker-publish.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/yattee/yattee-server)](https://hub.docker.com/r/yattee/yattee-server)
[![Docker Image Size](https://img.shields.io/docker/image-size/yattee/yattee-server/latest)](https://hub.docker.com/r/yattee/yattee-server)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org)
[![Platforms](https://img.shields.io/badge/Platforms-amd64%20%7C%20arm64-lightgrey.svg)]()

A self-hosted API server powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) that manages data extraction from YouTube and other video sites. Designed for use with [Yattee](https://github.com/yattee/yattee).

> **About this build:** This README documents the customized, cache-first Yattee Server build, incorporating the changes in `COMBINED_STREAM_PATCH.md`, `FEED_FIX_NOTES.md`, `REQUEST_PARITY_PATCH.md`, `CACHE_PATCH_NOTES.md`, `VIDEO_CACHE_NOTES.md`, `VIDEO_CACHE_AUDIT.md`, and `SWR_CACHE_NOTES.md`. The badges above refer to the upstream project, **not** to this customized build. The implementation was tested locally and key playback/cache/feed behavior was observed in the user's Docker logs on **November 7, 2026**. Details and unverified behaviors are distinguished below.

## Features

- **Multi-site extraction** — YouTube and other sites supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp), with per-site credentials.
- **Layered metadata extraction** — InnerTube, optional Invidious, and yt-dlp with fallback behavior.
- **Playback-first video API** — Return previously cached video details immediately, while updating metadata/recommendations separately in the background.
- **Combined-stream preference** — Prefer progressive video+audio formats over separate adaptive video/audio tracks when a combined format is available.
- **Persistent media cache** — Store complete media and reusable partial byte ranges; serve cached seeks and playback across restarts.
- **Persistent description and comments caches** — Indefinite description storage and configurable comments refresh, including stale-while-revalidate for previously cached comments.
- **Persistent subscription feed** — Return cached feed entries promptly; refresh only due channels using successful-fetch timestamps stored in SQLite.
- **Lazy channel avatars** — Refresh on channel visits rather than making feed/avatar image requests trigger lookups.
- **Stream proxy** — Fast yt-dlp-backed downloads and a byte-range `/proxy/relay` with yt-dlp-aligned upstream request headers.
- **Admin panel** — Setup wizard, settings, user management, channel status, and manual refresh controls.
- **Authentication** — HTTP Basic Auth, HMAC-signed media URLs, and rate limiting.
- **Optional Invidious integration** — Trending, popular, search suggestions, comments, captions, thumbnails, and avatars.
- **Search and channels** — Search filters, channel videos, playlists, shorts, streams, and more.
- **Invidious-compatible API** — API endpoints usable by compatible clients.
- **Optional egress proxy** — HTTP/SOCKS egress for configured YouTube-bound services; no VPN/WARP dependency is required for these cache features.
- **Docker deployment** — Persistent data mounts, environment configuration, and optional automated provisioning.

## Current deployment status (November 7, 2026)

The following behaviors are **confirmed in the supplied live Docker logs**, not just in local tests:

| Area | Observed result | Evidence in logs |
| --- | --- | --- |
| Combined playback | Progressive format selected; adaptive video tracks suppressed when a combined stream exists | `[Videos] Prefer combined ... 1 muxed streams, suppressed 18 adaptive video tracks` |
| Full media persistence | The same cached `itag=18` video remains playable after container recreation | `[VideoCache] HIT LEdN7vPkpdU:18` |
| Cache-only playback | A cached video can play from a newly signed local URL, without a fresh GoogleVideo download for its media bytes | `[VideoCache] HIT-LOCAL LEdN7vPkpdU:18` and `GET /proxy/cached/LEdN7vPkpdU/18 ... 206` |
| Video information caching | Video information is stored, then subsequent requests return cache hits | `[VideoInfoCache] STORED ...` and `[VideoInfoCache] HIT LEdN7vPkpdU (9 formats)` |
| Deferred recommendations | Initial playback-first data was stored with zero recommendations, then enriched in the background | `STORED ... (0 recommendations)` followed by `STORED ... (21 recommendations)` |
| Feed policy | First request served 138 cached channels; second request queued only stale channels | `FIRST-REQUEST cache-only (138 channels)` then `SECOND-REQUEST queued=28 fresh=110 ... interval=360min` |
| Media retention settings | A 30-day retention and persistent `/data` path are active | `[VideoCache] Settings: days=30 directory=/data/cache/videos enabled=True` |
| Startup cleanup | Cleanup ran successfully; no entries happened to be expired | `Startup cleanup: removed 0 expired videos and 0 incomplete entries (retention=30 days)` |

**Not established by those logs:** a successful *partial-range cache hit* during live playback; actual deletion of expired entries; or automatic recovery for every upstream 403. Partial caching, expiry, and fallback logic have local regression coverage, but should not be described as fully live-verified. Background feed requests have occasionally encountered InnerTube `browse` timeouts and retries.

## Quick start

### Local development

```bash
git clone https://github.com/yattee/yattee-server.git
cd yattee-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Open `http://localhost:8080` to run the initial setup, assuming the local-development port is unchanged.

**Note:** Cloning the upstream repository alone does **not** install the customized changes described here. Use the customized source/build containing these patches.

### Docker

Copy and configure the environment file, mount persistent storage, and start the customized build:

```bash
cp .env.example .env
# Edit .env; see the settings below.
docker compose up -d --build
```

The usual container service port is `8085`, subject to the configured bind address and Compose mapping. In the existing deployment the build is rebuilt/recreated with `dockerb yattee` and an `env_file` at `./yattee/.env`.

### Recommended `.env` for the current setup

These are the values used/recommended by the consolidated patches, **not** a replacement for the rest of your existing `.env`:

```dotenv
# Persistent paths
DATA_DIR=/data

# Video format and relay
PREFER_COMBINED_STREAMS=true
HTTP_CHUNK_SIZE=10485760

# Persistent media cache: complete and partial files
VIDEO_CACHE_DIR=/data/cache/videos
VIDEO_CACHE_DAYS=30

# Playback-first, stale-while-revalidate video information
VIDEO_INFO_CACHE_DIR=/data/cache/video_info
VIDEO_INFO_REFRESH_MINUTES=0

# Description and comments
VIDEO_DESCRIPTION_CACHE_DIR=/data/cache/video_descriptions
COMMENTS_CACHE_DIR=/data/cache/comments
COMMENTS_CACHE_MINUTES=360

# Feed refresh (360 minutes = 6 hours)
FEED_REFRESH_MINUTES=360
FEED_STARTUP_DELAY_SECONDS=300

# Lazy avatar cache
AVATAR_CACHE_DIR=/data/cache/avatars
AVATAR_REFRESH_DAYS=14
```

| Setting | Current behavior |
| --- | --- |
| `DATA_DIR=/data` | Stores persistent state in the directory mounted to the host. |
| `PREFER_COMBINED_STREAMS=true` | Prefer combined video+audio; `false` restores adaptive video choices. |
| `HTTP_CHUNK_SIZE=10485760` | 10 MiB **maximum** upstream relay chunk; individual requests use slightly varied sizes. This supersedes the old 1 MiB recommendation. |
| `VIDEO_CACHE_DIR` | Where complete and partial playback data are stored, separate from `/download`. |
| `VIDEO_CACHE_DAYS=30` | Remove media entries unused for 30 days **on startup**; `0` disables video-cache reads/writes. |
| `VIDEO_INFO_CACHE_DIR` | Persistent video-details responses used for instant revisits. |
| `VIDEO_INFO_REFRESH_MINUTES=0` | A cached response is returned immediately, and a deduplicated background metadata refresh may run on each visit. Positive values throttle refreshes to that many minutes. |
| `VIDEO_DESCRIPTION_CACHE_DIR` | First successful nonempty descriptions persist indefinitely in the dedicated description cache. |
| `COMMENTS_CACHE_MINUTES=360` | Six-hour comments freshness, with stale cached comments served while refreshing; `0` disables this comments cache. |
| `FEED_REFRESH_MINUTES=360` | Due threshold **and** periodic scheduler interval; successful `last_fetch` survives restarts. |
| `FEED_STARTUP_DELAY_SECONDS=300` | Five-minute delay before the initial scheduled feed sweep; it does not postpone serving cached feed entries. |
| `AVATAR_REFRESH_DAYS=14` | Per-channel avatar freshness interval when a channel is visited. |

## Playback behavior

### Prefer combined video/audio formats

When `PREFER_COMBINED_STREAMS=true` (the default in this customized build):

1. `/api/v1/videos/{id}` and `/api/v1/extract` offer playable progressive (muxed) video+audio formats first, preferring YouTube `itag 18` among combined formats.
2. If a playable combined stream exists, video-only adaptive tracks are omitted from the advertised video choices. **Audio-only** tracks are retained for audio-only usage.
3. If no combined stream is available, or the video is live, the existing adaptive/live formats remain available.
4. If an InnerTube merge omitted a combined `itag 18` already extracted by yt-dlp, the server restores it from the existing yt-dlp data without an extra extraction.

This avoids the previously observed 360p `itag 134` video-only plus separate `140-drc` combination, which was returning HTTP 403/502 in the user's playback logs, while combined `itag 18` subsequently played successfully.

**Limitations:** This preference may reduce offered video resolutions, since combined formats are often more limited than adaptive ones. The server cannot swap one format's bytes for another inside an already-started HTTP response. Recovering from a failed stream requires a **new** video/stream request by the client; fully automatic client recovery is not guaranteed.

### Upstream request parity and relay

The `/proxy/relay` endpoint supports HTTP byte ranges and seeking. The request-parity changes:

- Carry an allowlisted subset of yt-dlp's format-specific, non-sensitive `http_headers` in the HMAC-protected relay parameters.
- Apply these headers when contacting GoogleVideo and update them alongside any re-extracted media URL.
- Vary request lengths to approximately **95–100%** of `HTTP_CHUNK_SIZE`, rather than requesting exact fixed boundaries.
- Keep sensitive cookies, Authorization headers, and private credentials out of the forwarded header parameter; previously signed relay URL shapes remain supported.
- Retain existing error handling and retries, including smaller ranges for certain upstream 403 responses.

A server can return HTTP `206 Partial Content` for bytes served **locally**; `206` alone does not establish whether it contacted YouTube. A genuinely failed upstream request after streaming starts may still interrupt playback.

## Persistent media cache

### Complete and partial playback

Cache keys use **video ID plus format ID** (for example `LEdN7vPkpdU_18`), not the expiring signed GoogleVideo URL. The cache writes only media bytes obtained during playback; it does **not** proactively download the unwatched remainder.

- **Incomplete:** a sparse `.part` media file plus `.json` metadata records only successfully downloaded, durable byte ranges. Sparse holes are never served as if they contained content.
- **Cache-only seek:** if every requested byte already exists, serve it from disk (`PARTIAL-HIT`) without an upstream request.
- **Mixed seek:** reuse cached ranges and fetch only missing ranges, saving newly received bytes (`PARTIAL`).
- **Complete:** once all bytes are present, the cache publishes a `.media` file with `.json` metadata and `completed_at` (`HIT`).
- **Restart:** index timestamps and media files survive as long as `/data` is persistent. Access timestamps control the configured retention window for both full and partial media.
- **Cleanup:** expired complete and partial media are removed at startup only, without touching downloads, feed data, avatars, descriptions, or comments.
- **Format isolation:** `itag 18` cache bytes are not substituted for `itag 134`/`140-drc` or any other format.
- **Security:** local and relay playback still require valid signed URL authorization.

The audit patch also recognizes media reported upstream as `application/octet-stream` **only when** a verified GoogleVideo `/videoplayback` URL identifies a media MIME type; this avoids silently bypassing caching for valid MP4 streams.

### Cached video playback URLs

For **fully cached** media, `/api/v1/videos/{id}` can return a newly signed **local** `/proxy/cached/{video_id}/{itag}` URL instead of relying on a stale GoogleVideo signature. Playback can therefore use stored bytes without extracting a new upstream media URL. The live logs confirmed `HIT-LOCAL` with HTTP 206 from this endpoint after a restart.

For **partially cached** media, any missing bytes still require a working upstream stream URL. If that URL has expired, fresh extraction may be required. Live/HLS media and streams with unknown lengths are not stored by this cache.

### Storage considerations

The media cache has **no configured quota or per-video disk cap** in these patches. Monitor storage growth:

```bash
docker exec yattee du -sh /data/cache/videos
docker exec yattee ls -lh /data/cache/videos
```

The range index is designed for one Yattee server process/worker, using positioned writes and atomic JSON updates. Do not assume it is safe to run multiple workers writing the same cache files.

## Playback-first video information and metadata

### Stale-while-revalidate video details

- A request for an **already cached** video returns a saved, validated `/api/v1/videos/{id}` response immediately when it has usable playback information. A fully cached format receives a fresh signed local media URL.
- A deduplicated background task refreshes recommendations, author details, descriptions, and other metadata for a future request. Upstream failures should preserve the last known good response.
- On a **first uncached visit**, the server can return yt-dlp playback formats before the supplementary recommendation (`/next`) enrichment finishes. Initial extraction may still take time because no cached playback information exists yet.
- The updated recommendations appear on the **next request/visit**, not automatically in the current already-rendered client page.
- Live and upcoming streams deliberately bypass persistent video-info caching.

`VIDEO_INFO_REFRESH_MINUTES=0` allows a background refresh on each cached visit; a positive value limits how often one is queued. This is separate from caching media bytes and from the dedicated permanent description cache.

### Description and comments

- **Descriptions:** the first successfully fetched nonempty description (and available HTML) is saved persistently. The dedicated description-cache note specifies **no automatic expiry**; to force replacement of that stored snapshot, delete its corresponding cache file. This is distinct from the broader video-info metadata refresh policy.
- **Comments:** cached per `(video_id, continuation)` page. Successful results persist across restarts with a UTC `fetched_at`; error/negative results are not cached.
- **Comment refresh:** fresh data is returned immediately. Expired **existing** comments can be served stale while a background refresh proceeds. If there is no stored page, the first fetch still requires network access.

## Feed refresh and persistent timestamps

A prior bug meant `database.get_all_watched_channels()` did not select the existing `feed_fetch_status.last_fetch` timestamp, causing the scheduler to consider channels unfetched. The query now includes this persisted SQLite field. **No database migration** is required for that change.

### Two-request cache-first behavior (no client update)

1. On the **first normal** `POST /api/v1/feed` for a specific channel set after server startup, return cached feed entries immediately without refreshing already-cached channels.
2. On the **second non-paginated** request for the same set, again return cached entries immediately; queue background refreshes **only** for channels whose last successful fetch is at least `FEED_REFRESH_MINUTES` old.
3. On later requests, continue applying the same age check. Skip fresh channels, avoid duplicate in-flight jobs, and back off briefly (up to five minutes) after recent failures.
4. New subscriptions without cached entries may be queued in the background on the first request. Pagination requests with `offset > 0` do not count as a second request.
5. The periodic feed task remains enabled, uses the same age threshold and schedule, and waits `FEED_STARTUP_DELAY_SECONDS` before its startup sweep. Failed refreshes do **not** overwrite the last *successful* fetch timestamp.

The admin panel's explicit **Refresh all** action is a separate manual operation and, per `FEED_FIX_NOTES.md`, can bypass the freshness interval. Ordinary client feed polling and the two-request heuristic **do not** bypass it.

**Important:** Since the client sends no explicit `refresh` flag, the server cannot reliably determine whether a second request is a deliberate pull-to-refresh or another automatic request. This is intentionally a **best-effort** convention, constrained by `FEED_REFRESH_MINUTES`, not guaranteed gesture detection.

## Lazy avatar caching

Channel avatars and their metadata are persisted under `AVATAR_CACHE_DIR`, with an age threshold set by `AVATAR_REFRESH_DAYS`. Channel page/video-tab visits can trigger an avatar refresh when due. Normal subscriptions feed and `/avatar` image requests do not fetch avatars themselves. Newly encountered channels may temporarily display a placeholder until a channel visit populates their avatar cache. The cache survives restarts without a schema migration.

## Diagnostics and troubleshooting

### Cache and refresh logs

```bash
# Media cache activity
docker logs yattee 2>&1 | grep '\[VideoCache\]' | tail -50

# Video-info cache and deferred enrichment
docker logs yattee 2>&1 | grep -E '\[VideoInfoCache\]|Playback-first' | tail -50

# Combined-stream selection and feed policy
docker logs yattee 2>&1 | grep -E 'Prefer combined|FIRST-REQUEST|SECOND-REQUEST|channels due' | tail -50
```

| Log event | Meaning |
| --- | --- |
| `[VideoCache] LOOKUP/MISS` | No reusable cache found for the requested range. |
| `[VideoCache] SAVED` / `Completed` | Data persisted; `Completed` means all media bytes are available. |
| `[VideoCache] PARTIAL` | Requested bytes combined cached and downloaded ranges. |
| `[VideoCache] PARTIAL-HIT` | Requested range served entirely from an incomplete cache. |
| `[VideoCache] HIT` | Complete video cache hit. |
| `[VideoCache] HIT-LOCAL` | Served through the local `/proxy/cached/` URL. |
| `[VideoCache] BYPASS` | Caching intentionally skipped; inspect the reason. |
| `[VideoInfoCache] HIT` | Previously stored video information returned without blocking on fresh extraction. |
| `[VideoInfoCache] STORED ... (N recommendations)` | Cached video information written, possibly followed by enrichment. |
| `[Feed] FIRST-REQUEST` | Cached feed-only first request for the channel set. |
| `[Feed] SECOND-REQUEST queued=...` | Due channels queued in background; freshness interval still enforced. |

If a media `LOOKUP/MISS` is not followed by a `SAVED` event, bytes may not have been persisted (for example if streaming was interrupted or the cache was bypassed). Inspect `BYPASS` and check the actual cache directory configured in `.env`.

### Known caveats

- Initial playback for **uncached** videos still needs live format extraction; initial comments with no cache also need a network fetch.
- No server patch can force the client to change formats *inside* an existing HTTP media response. Some client-side loading or re-request behavior remains outside the server's control.
- Partial-range cache reuse, old-entry deletion, and automatic recovery from every 403 are **not yet confirmed in the supplied live logs** even though regression tests cover important cases.
- Background feed `browse` requests have shown timeouts and retries under load.
- Full integration tests were not completely clean in the development environment: earlier test runs were affected by missing `bcrypt` and uninitialized SQLite settings fixtures. The most recent consolidated build reported **100 targeted tests passed**; this does not constitute a clean full-suite or production certification.

## Configuration

Yattee Server supports startup environment variables in `.env` and runtime settings in the admin panel/database. Other network-related variables include `YT_EGRESS_PROXY` for optional HTTP/SOCKS egress and `SSRF_EXTRA_ALLOWED_CIDRS` for extra SSRF-guard CIDR allowances (for example, a LAN Invidious instance). See [docs/configuration.md](docs/configuration.md) for the broader configuration reference.

## API endpoints

The server provides video extraction, search, channels, playlists, comments, subscriptions feed, stream proxy, captions, thumbnails, and more. Relevant cache-first endpoints include:

| Endpoint | Role |
| --- | --- |
| `GET /api/v1/videos/{id}` | Playback-first, persistent video information with background metadata refresh. |
| `GET /api/v1/extract` | Extraction with combined-stream preference where applicable. |
| `POST /api/v1/feed` | Cached feed response; second-request due-refresh heuristic. |
| `GET /api/v1/comments/{id}` | Cached comments with stale-while-revalidate. |
| `GET /proxy/relay` | Signed, range-aware upstream relay; records complete/partial media cache. |
| `GET /proxy/cached/{video_id}/{itag}` | Signed local playback URL for a fully cached format. |

See [docs/api.md](docs/api.md) for the general API reference. The customized endpoints and behaviors above may not yet be reflected in the upstream API documentation.

## Admin panel

The web admin panel at `/admin` includes:

- First-run setup wizard and optional Invidious connection.
- Runtime settings, user administration, network/egress configuration, and site credentials.
- Watched-channel feed status and a **Refresh all** manual action.
- A browser watch page for testing video URLs.

## Authentication

- **HTTP Basic Auth** — General API/admin access once setup is complete.
- **Signed media URLs** — HMAC-authenticated relay and local cached playback URLs, so players can retrieve media without embedding user credentials in the URL.
- **Rate limiting** — Failed authentication attempts are limited per IP.

Manage users in the admin panel. The relay forwards an allowlist of format headers, not cookies or Authorization values.

## Requirements

- **Python 3.12+**
- **yt-dlp** for extraction
- **Deno** for YouTube JavaScript challenge solving
- **ffmpeg** for media processing

## Docker Compose and persistence

For a fresh Docker deployment, align the configured paths with the mounts; this example uses the customized build's `/data` and `/download` paths:

```yaml
services:
  yattee-server:
    build: .
    container_name: yattee-server
    ports:
      - "8085:8085"
    volumes:
      - ./data:/data
      - ./downloads:/download
    env_file:
      - .env
    restart: unless-stopped
```

```dotenv
DATA_DIR=/data
# If your installation needs to override the existing download location:
# DOWNLOAD_DIR=/download
```

In the existing deployment, the host bind mounts are:

```yaml
volumes:
  - /mnt/hd1/docker/yattee/data:/data
  - /mnt/hd1/docker/yattee/download:/download
```

The media cache, video-information cache, descriptions, comments, avatar cache, and SQLite feed timestamps are all persisted via `/data`. The separate `/download` mount holds existing user downloads and **is not** the media playback cache. Preserve your existing networks, reverse-proxy labels, service name, and `.env` settings when installing patches.

### Auto-provisioning

For automated setups, `ADMIN_USERNAME` and `ADMIN_PASSWORD` can provision or update an administrator on startup; `INVIDIOUS_INSTANCE_URL` can configure an optional backing Invidious instance.


## License

MIT
