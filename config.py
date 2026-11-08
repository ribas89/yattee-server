"""Configuration for Yattee Server.

Startup-only settings are configured here via environment variables.
Runtime settings are managed via the admin panel and stored in the database.
"""

import os

# Server settings (startup-only)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# Data directory (for database, temp files, etc.)
DATA_DIR = os.getenv("DATA_DIR", "data")

# Download directory (for proxied video downloads)
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "")

# Upstream HTTP range size (bytes) used by /proxy/relay for video playback.
# Default: 1 MiB, which works with googlevideo streams that reject 8-10 MiB ranges.
# Set HTTP_CHUNK_SIZE in .env or Docker environment and recreate the container.
def _parse_http_chunk_size() -> int:
    raw = os.getenv("HTTP_CHUNK_SIZE", "1048576").strip()
    try:
        size = int(raw)
    except ValueError as exc:
        raise ValueError("HTTP_CHUNK_SIZE must be an integer number of bytes") from exc
    if not 65536 <= size <= 10485760:
        raise ValueError("HTTP_CHUNK_SIZE must be between 65536 and 10485760 bytes")
    return size


HTTP_CHUNK_SIZE = _parse_http_chunk_size()

# Prefer a single progressive video+audio track over separate adaptive tracks.
# When available, suppress adaptive *video* tracks in /api/v1/videos (audio-only
# tracks remain available for the client's audio-only playback mode).
# When no progressive stream exists, the original adaptive formats remain.
PREFER_COMBINED_STREAMS = os.getenv("PREFER_COMBINED_STREAMS", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

# Persistent complete-video cache for /proxy/relay. Stored on the /data volume.
# Retention is measured from the most recent playback of a completed video.
# Set VIDEO_CACHE_DAYS=0 to disable new caching and cached playback.
def _video_cache_days() -> int:
    raw = os.getenv("VIDEO_CACHE_DAYS", "30").strip()
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError("VIDEO_CACHE_DAYS must be an integer") from exc
    if not 0 <= days <= 3650:
        raise ValueError("VIDEO_CACHE_DAYS must be between 0 and 3650")
    return days

VIDEO_CACHE_DAYS = _video_cache_days()
VIDEO_CACHE_DIR = os.getenv("VIDEO_CACHE_DIR", os.path.join(DATA_DIR, "cache", "videos"))

# Avatar updates happen only when a channel page/videos endpoint is visited.
# Use /data/cache/avatars with a /data bind mount; no database migration needed.
def _avatar_refresh_days() -> int:
    raw = os.getenv("AVATAR_REFRESH_DAYS", "14")
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError("AVATAR_REFRESH_DAYS must be an integer") from exc
    if not 1 <= days <= 365:
        raise ValueError("AVATAR_REFRESH_DAYS must be between 1 and 365")
    return days

AVATAR_REFRESH_DAYS = _avatar_refresh_days()
AVATAR_CACHE_DIR = os.getenv("AVATAR_CACHE_DIR", os.path.join(DATA_DIR, "cache", "avatars"))


# Persistent metadata caches (files under DATA_DIR/cache).
# Descriptions have no TTL; video stream URLs are never cached here.
VIDEO_DESCRIPTION_CACHE_DIR = os.getenv(
    "VIDEO_DESCRIPTION_CACHE_DIR", os.path.join(DATA_DIR, "cache", "video_descriptions")
)
COMMENTS_CACHE_DIR = os.getenv(
    "COMMENTS_CACHE_DIR", os.path.join(DATA_DIR, "cache", "comments")
)

def _comments_cache_minutes() -> int:
    raw = os.getenv("COMMENTS_CACHE_MINUTES", "360").strip()
    try:
        minutes = int(raw)
    except ValueError as exc:
        raise ValueError("COMMENTS_CACHE_MINUTES must be an integer") from exc
    if not 0 <= minutes <= 10080:
        raise ValueError("COMMENTS_CACHE_MINUTES must be between 0 and 10080")
    return minutes

COMMENTS_CACHE_MINUTES = _comments_cache_minutes()

# Instant cached video info, revalidated asynchronously. 0 means revalidate on
# each cached-video entry (deduplicated while a refresh is running).
VIDEO_INFO_CACHE_DIR = os.getenv("VIDEO_INFO_CACHE_DIR", os.path.join(DATA_DIR, "cache", "video_info"))
def _video_info_refresh_minutes():
    raw = os.getenv("VIDEO_INFO_REFRESH_MINUTES", "0")
    try:
        minutes = int(raw)
    except ValueError as exc:
        raise ValueError("VIDEO_INFO_REFRESH_MINUTES must be an integer") from exc
    if not 0 <= minutes <= 10080:
        raise ValueError("VIDEO_INFO_REFRESH_MINUTES must be between 0 and 10080")
    return minutes
VIDEO_INFO_REFRESH_MINUTES = _video_info_refresh_minutes()

# Encryption key for sensitive credentials (auto-generated if not set)
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CREDENTIALS_ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")

# CORS settings
# Comma-separated list of allowed origins (e.g., "https://app.example.com,https://admin.example.com")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "")

# Allow all origins (development mode) - credentials will be DISABLED in this mode
CORS_ALLOW_ALL = os.getenv("CORS_ALLOW_ALL", "false").lower() in ("true", "1", "yes")

# Allow credentials (cookies, authorization headers) - only works with specific origins
CORS_ALLOW_CREDENTIALS = os.getenv("CORS_ALLOW_CREDENTIALS", "true").lower() in ("true", "1", "yes")

# Debug mode (enables auto-reload in development)
DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

# Secure cookies (set to false only for local development without HTTPS)
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "true").lower() in ("true", "1", "yes")

# Skip TLS certificate verification in yt-dlp (not recommended for production)
YTDLP_SKIP_TLS_VERIFY = os.getenv("YTDLP_SKIP_TLS_VERIFY", "false").lower() in ("true", "1", "yes")

# HTTP proxy for YouTube-bound egress (yt-dlp, InnerTube, Invidious fallback).
# Format: http://[user:pass@]host:port or socks5://host:port
YT_EGRESS_PROXY = os.getenv("YT_EGRESS_PROXY") or None

# Force IP family for YouTube-bound egress: "auto", "ipv4" or "ipv6".
# Seeds the yt_ip_family setting on startup (see env_provisioning.py).
YT_IP_FAMILY = os.getenv("YT_IP_FAMILY") or None

# PO token provider (bgutil): "true"/"false" seeds the yt_pot_enabled setting
# on startup; YT_POT_PROVIDER_URL points at an external provider instead of
# the bundled one (see env_provisioning.py).
YT_POT_ENABLED = os.getenv("YT_POT_ENABLED") or None
YT_POT_PROVIDER_URL = os.getenv("YT_POT_PROVIDER_URL") or None

# Additional CIDR ranges to permit through the SSRF guard.
# Comma-separated list of IPv4/IPv6 networks in CIDR notation, e.g.
# "10.20.30.0/24,fd00::/8" — useful when a backing service (Invidious /
# yattee-server companion) lives on the LAN and returns stream URLs that
# point at private IPs. Entries are parsed at startup; malformed values
# are logged and dropped. Loopback is always blocked regardless.
SSRF_EXTRA_ALLOWED_CIDRS = os.getenv("SSRF_EXTRA_ALLOWED_CIDRS", "")

# Auto-provisioning (optional, for automated deployments)
# When ADMIN_USERNAME and ADMIN_PASSWORD are both set, an admin user is auto-created/updated on startup
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
# When set, configures the Invidious instance URL and enables the proxy on startup
INVIDIOUS_INSTANCE_URL = os.getenv("INVIDIOUS_INSTANCE_URL")

# Delays the initial bulk video-feed refresh (independent from avatars).
def _feed_startup_delay() -> int:
    raw = os.getenv('FEED_STARTUP_DELAY_SECONDS', '300')
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValueError('FEED_STARTUP_DELAY_SECONDS must be an integer') from exc
    if not 0 <= seconds <= 86400:
        raise ValueError('FEED_STARTUP_DELAY_SECONDS must be between 0 and 86400')
    return seconds

FEED_STARTUP_DELAY_SECONDS = _feed_startup_delay()

# Minimum age of a successful channel-feed refresh before checking YouTube again.
# None means use the interval configured in the admin settings (default 360 min).
def _feed_refresh_minutes():
    raw = os.getenv("FEED_REFRESH_MINUTES", "").strip()
    if not raw:
        return None
    try:
        minutes = int(raw)
    except ValueError as exc:
        raise ValueError("FEED_REFRESH_MINUTES must be an integer") from exc
    if not 5 <= minutes <= 10080:
        raise ValueError("FEED_REFRESH_MINUTES must be between 5 and 10080")
    return minutes


FEED_REFRESH_MINUTES = _feed_refresh_minutes()

