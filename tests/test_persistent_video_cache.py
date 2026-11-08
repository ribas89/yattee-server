"""Persistent complete-video caching, seek behavior and relay cache hits."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import config
import video_cache
from routers.proxy import _relay

VIDEO_ID = "wh6dlpO-f3A"
FORMAT = "18"
DATA = bytes(i % 251 for i in range(163_871))
UPSTREAM = "https://rr2.googlevideo.com/videoplayback?itag=18&clen=163871"


@pytest.fixture
def disk(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VIDEO_CACHE_DIR", str(tmp_path / "videos"))
    monkeypatch.setattr(config, "VIDEO_CACHE_DAYS", 30)
    yield tmp_path / "videos"
    assert not video_cache._active


def record(offset: int, payload: bytes):
    writer = video_cache.begin(VIDEO_ID, FORMAT, len(DATA), "video/mp4", offset)
    assert writer is not None
    writer.write(payload)
    writer.close()


@pytest.mark.asyncio
async def test_full_video_replay_ranges_and_restart(disk):
    record(0, DATA)
    cached = video_cache.lookup(VIDEO_ID, FORMAT)
    assert cached and cached.path.read_bytes() == DATA
    assert cached.size == len(DATA)
    assert cached.content_type == "video/mp4"
    assert (disk / f"{VIDEO_ID}_{FORMAT}.json").exists()
    # Disk-only data: no process memory cache needed on restart.
    assert video_cache.lookup(VIDEO_ID, FORMAT) == cached
    hit = video_cache.cached_response(cached, "bytes=250-1023")
    assert hit.status_code == 206
    assert hit.headers["content-length"] == "774"
    assert hit.headers["content-range"] == f"bytes 250-1023/{len(DATA)}"
    result = b"".join([part async for part in hit.body_iterator])
    assert result == DATA[250:1024]
    suffix = video_cache.cached_response(cached, "bytes=-39")
    assert suffix.status_code == 206
    assert b"".join([part async for part in suffix.body_iterator]) == DATA[-39:]
    invalid = video_cache.cached_response(cached, "bytes=200000-")
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{len(DATA)}"


def test_sparse_ranges_do_not_publish_until_complete(disk):
    record(0, DATA[:32000])
    assert video_cache.lookup(VIDEO_ID, FORMAT) is None
    record(80000, DATA[80000:])
    assert video_cache.lookup(VIDEO_ID, FORMAT) is None
    record(32000, DATA[32000:80000])
    complete = video_cache.lookup(VIDEO_ID, FORMAT)
    assert complete is not None and complete.path.read_bytes() == DATA
    assert not (disk / f"{VIDEO_ID}_{FORMAT}.part").exists()


def test_failed_incomplete_cache_and_retention(disk):
    record(0, DATA[:40000])
    assert video_cache.lookup(VIDEO_ID, FORMAT) is None
    index = disk / f"{VIDEO_ID}_{FORMAT}.json"
    meta = json.loads(index.read_text())
    meta["last_accessed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    index.write_text(json.dumps(meta))
    # Recent partial data is now retained for the same 30 days as full videos.
    assert video_cache.cleanup_expired() == (0, 0)
    assert index.exists()
    meta["last_accessed_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    index.write_text(json.dumps(meta))
    assert video_cache.cleanup_expired() == (0, 1)
    assert not index.exists()
    record(0, DATA)
    meta = json.loads(index.read_text())
    meta["last_accessed_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    index.write_text(json.dumps(meta))
    assert video_cache.cleanup_expired() == (1, 0)
    assert not list(disk.iterdir())


def test_active_and_old_video_replaced_safely(disk):
    record(0, DATA[:1000])
    one = video_cache.begin(VIDEO_ID, FORMAT, len(DATA), "video/mp4", 1000)
    assert one is not None
    assert video_cache.begin(VIDEO_ID, FORMAT, len(DATA) + 2, "video/mp4", 0) is None
    one.write(DATA[1000:])
    one.close()
    assert video_cache.lookup(VIDEO_ID, FORMAT) is not None
    # A completed file remains usable even if another stream announces
    # an incompatible byte count for the same format.
    writer = video_cache.begin(VIDEO_ID, FORMAT, len(DATA) + 2, "video/mp4", 0)
    assert writer is None  # valid completed media must remain available until expiry


def test_disabled_cache(disk, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_CACHE_DAYS", 0)
    assert video_cache.begin(VIDEO_ID, FORMAT, len(DATA), "video/mp4", 0) is None
    assert video_cache.lookup(VIDEO_ID, FORMAT) is None


def network(request: httpx.Request):
    start, end = map(int, request.headers["range"][6:].split("-"))
    if start >= len(DATA):
        return httpx.Response(416)
    end = min(end, len(DATA) - 1)
    return httpx.Response(206, headers={
        "Content-Range": f"bytes {start}-{end}/{len(DATA)}",
        "Content-Length": str(end - start + 1),
        "Content-Type": "video/mp4",
    }, stream=httpx.ByteStream(DATA[start:end + 1]), request=request)


def req(rng="bytes=0-"):
    return Request({"type": "http", "method": "GET", "headers": [(b"range", rng.encode())],
                    "path": "/proxy/relay", "query_string": b"", "scheme": "http",
                    "server": ("localhost", 80)})


def params():
    signed = _relay.signed_relay_url("http://localhost", UPSTREAM, video_id=VIDEO_ID, itag=FORMAT)
    result = {k: v[0] for k, v in parse_qs(urlsplit(signed).query).items()}
    result["exp"] = int(result["exp"])
    return result


@pytest.mark.asyncio
async def test_relay_download_then_replay_without_upstream(disk, monkeypatch):
    upstream_calls = []
    def handler(request):
        upstream_calls.append(request)
        return network(request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    httpx_proxy = SimpleNamespace(**{k: getattr(httpx, k) for k in (
        "HTTPStatusError", "AsyncHTTPTransport", "Timeout", "RequestError",
        "RemoteProtocolError", "ReadError", "WriteError", "ConnectError", "ReadTimeout")})
    httpx_proxy.AsyncClient = lambda **kw: client
    monkeypatch.setattr(_relay, "httpx", httpx_proxy)
    monkeypatch.setattr(_relay, "CHUNK_SIZE", 8192)
    monkeypatch.setattr(_relay, "is_safe_url", lambda url: True)
    monkeypatch.setattr(_relay, "get_settings", lambda: SimpleNamespace(
        effective_yt_egress_proxy=lambda: None, effective_ip_family=lambda: "auto"))
    monkeypatch.setattr(_relay.token_utils, "_get_signing_key", lambda: b"cache-test-key")

    resp = await _relay.relay(req(), **params())
    assert resp.status_code == 206
    assert resp.headers["x-yattee-video-cache"] == "MISS"
    assert b"".join([x async for x in resp.body_iterator]) == DATA
    assert video_cache.lookup(VIDEO_ID, FORMAT) is not None
    calls_before = len(upstream_calls)
    # A newly minted URL still matches the stable video/format key.
    new_params = params()
    new_upstream = UPSTREAM + "&new_signature=123"
    new_params["url"] = new_upstream
    new_params["sig"] = _relay._sign(new_upstream, new_params["exp"], VIDEO_ID, FORMAT)
    resp2 = await _relay.relay(req("bytes=500-1500"), **new_params)
    assert resp2.status_code == 206
    assert resp2.headers["x-yattee-video-cache"] == "HIT"
    assert b"".join([x async for x in resp2.body_iterator]) == DATA[500:1501]
    assert len(upstream_calls) == calls_before
    # No full cache should ever be returned without valid signature.
    bad = params()
    bad["sig"] = "tampered"
    with pytest.raises(HTTPException) as e:
        await _relay.relay(req(), **bad)
    assert e.value.status_code == 403


def test_partial_ranges_persist_and_never_serve_holes(disk):
    record(0, DATA[:4096])
    record(9000, DATA[9000:20000])
    assert video_cache.lookup(VIDEO_ID, FORMAT) is None
    snap = video_cache.lookup_partial(VIDEO_ID, FORMAT)
    assert snap is not None
    assert snap.ranges == ((0, 4096), (9000, 20000))
    assert video_cache.split_ranges(snap, 1000, 18000) == [
        (True, 1000, 4096), (False, 4096, 9000), (True, 9000, 18000),
    ]
    # All-in-cache seek is a hit; spanning the hole requires an upstream fetch.
    assert video_cache.cached_partial_response(VIDEO_ID, FORMAT, "bytes=9000-15000") is not None
    assert video_cache.cached_partial_response(VIDEO_ID, FORMAT, "bytes=4000-9500") is None
    assert video_cache.cached_partial_response(VIDEO_ID, FORMAT, "bytes=0-") is None
    # The sparse file has zero-filled holes. Never serve them as media.
    snap2 = video_cache.lookup_partial(VIDEO_ID, FORMAT)
    assert snap2.ranges == snap.ranges  # files on disk survive a logical restart


@pytest.mark.asyncio
async def test_partial_seek_hit_does_not_contact_upstream(disk, monkeypatch):
    record(0, DATA[:8192])
    def fail_network(request):
        raise AssertionError("Partial-cache seek should not contact GoogleVideo")
    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_network))
    patch_relay(monkeypatch, client)
    resp = await _relay.relay(req("bytes=1234-7000"), **params())
    assert resp.status_code == 206
    assert resp.headers["x-yattee-video-cache"] == "PARTIAL-HIT"
    assert b"".join([c async for c in resp.body_iterator]) == DATA[1234:7001]


@pytest.mark.asyncio
async def test_partial_mixed_replay_only_fetches_missing_bytes(disk, monkeypatch):
    record(0, DATA[:32000])
    record(90000, DATA[90000:130000])
    calls = []
    def handler(request):
        calls.append(request.headers["range"])
        return network(request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    patch_relay(monkeypatch, client)
    response = await _relay.relay(req("bytes=0-145000"), **params())
    assert response.status_code == 206
    assert response.headers["x-yattee-video-cache"] == "PARTIAL"
    result = b"".join([b async for b in response.body_iterator])
    assert result == DATA[:145001]
    for header in calls:
        a, b = map(int, header[6:].split("-"))
        if (a, b) == (0, 0):  # total-size metadata probe
            continue
        assert a >= 32000  # never refetched cached prefix
        assert b < 90000 or a >= 130000  # never refetched middle cached interval
    after = video_cache.lookup_partial(VIDEO_ID, FORMAT)
    assert after is not None
    assert after.ranges == ((0, 145001),)


@pytest.mark.asyncio
async def test_interrupted_relay_resume_after_restart(disk, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.headers["range"])
        return network(request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    patch_relay(monkeypatch, client)
    first = await _relay.relay(req("bytes=0-"), **params())
    # Client disconnects early. Only the received prefix is persisted.
    first_piece = await anext(first.body_iterator)
    assert first_piece == DATA[:len(first_piece)]
    await first.body_iterator.aclose()
    saved = video_cache.lookup_partial(VIDEO_ID, FORMAT)
    assert saved and saved.ranges == ((0, len(first_piece)),)
    previous_count = len(calls)
    again = await _relay.relay(req(f"bytes=0-{len(first_piece)-1}"), **params())
    assert b"".join([b async for b in again.body_iterator]) == first_piece
    assert len(calls) == previous_count
    # New request for the whole stream fills only the uncached portion.
    patch_relay(monkeypatch, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    second = await _relay.relay(req("bytes=0-"), **params())
    assert b"".join([b async for b in second.body_iterator]) == DATA
    assert video_cache.lookup(VIDEO_ID, FORMAT).path.read_bytes() == DATA


def patch_relay(monkeypatch, client):
    httpx_proxy = SimpleNamespace(**{k: getattr(httpx, k) for k in (
        "HTTPStatusError", "AsyncHTTPTransport", "Timeout", "RequestError",
        "RemoteProtocolError", "ReadError", "WriteError", "ConnectError", "ReadTimeout")})
    httpx_proxy.AsyncClient = lambda **kw: client
    monkeypatch.setattr(_relay, "httpx", httpx_proxy)
    monkeypatch.setattr(_relay, "CHUNK_SIZE", 8192)
    monkeypatch.setattr(_relay, "is_safe_url", lambda url: True)
    monkeypatch.setattr(_relay, "get_settings", lambda: SimpleNamespace(
        effective_yt_egress_proxy=lambda: None, effective_ip_family=lambda: "auto"))
    monkeypatch.setattr(_relay.token_utils, "_get_signing_key", lambda: b"cache-test-key")

@pytest.mark.asyncio
async def test_partial_response_safe_when_another_stream_completes_file(disk):
    # A client has obtained a partial response, but another client fills the
    # remaining bytes and renames .part to .media before it starts reading.
    record(0, DATA[:11000])
    response = video_cache.cached_partial_response(VIDEO_ID, FORMAT, "bytes=1000-10999")
    assert response is not None and response.status_code == 206
    record(11000, DATA[11000:])
    assert video_cache.lookup(VIDEO_ID, FORMAT) is not None
    assert b"".join([piece async for piece in response.body_iterator]) == DATA[1000:11000]

@pytest.mark.asyncio
async def test_generic_googlevideo_mime_caches_from_signed_mime(disk, monkeypatch):
    # Some GoogleVideo byte-range probes identify media as application/octet-stream.
    url = UPSTREAM + '&mime=video%2Fmp4'
    calls = []
    def handler(request):
        calls.append(request.headers['range'])
        resp = network(request)
        resp.headers['content-type'] = 'application/octet-stream'
        return resp
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    patch_relay(monkeypatch, client)
    signed = _relay.signed_relay_url('http://localhost', url, video_id=VIDEO_ID, itag=FORMAT)
    p = {k: v[0] for k,v in parse_qs(urlsplit(signed).query).items()}
    p['exp'] = int(p['exp'])
    first = await _relay.relay(req('bytes=0-'), **p)
    assert first.headers['x-yattee-video-cache'] == 'MISS'
    assert first.headers['content-type'] == 'video/mp4'
    assert b''.join([b async for b in first.body_iterator]) == DATA
    assert video_cache.lookup(VIDEO_ID, FORMAT) is not None
    count = len(calls)
    second = await _relay.relay(req('bytes=100-500'), **p)
    assert second.headers['x-yattee-video-cache'] == 'HIT'
    assert b''.join([b async for b in second.body_iterator]) == DATA[100:501]
    assert len(calls) == count


def test_cache_mime_fallback_does_not_trust_unrelated_paths_or_hosts():
    valid = 'https://rr1.googlevideo.com/videoplayback?mime=video%2Fmp4'
    assert _relay._media_cache_type(valid, 'application/octet-stream') == 'video/mp4'
    assert _relay._media_cache_type(valid, 'video/webm') == 'video/webm'
    assert _relay._media_cache_type('https://example.com/videoplayback?mime=video%2Fmp4',
                                    'application/octet-stream') == 'application/octet-stream'
    assert _relay._media_cache_type('https://www.youtube.com/watch?mime=video%2Fmp4',
                                    'application/octet-stream') == 'application/octet-stream'
