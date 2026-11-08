"""Focused regression tests for 1 MiB relay seeks and lazy persistent avatars."""

import asyncio
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import config
from avatar_cache import AvatarCache
from routers.proxy import _relay


@pytest.mark.asyncio
async def test_avatar_disk_cache_survives_restart_and_never_fetches_on_get(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'AVATAR_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(config, 'AVATAR_REFRESH_DAYS', 14)
    first = AvatarCache()
    monkeypatch.setattr(first, '_schedule_image', lambda *args, **kwargs: None)
    assert first.store_from_channel_info('UCsample', [{'url': 'https://example.com/image.jpg'}])
    new_process = AvatarCache()
    def fail(*args, **kwargs):
        raise AssertionError('reading cached avatars must not make network requests')
    monkeypatch.setattr(new_process, 'schedule_background_fetch', fail)
    assert await new_process.get('UCsample') == [{'url': 'https://example.com/image.jpg'}]
    assert not new_process._should_fetch('UCsample')
    assert len(list(tmp_path.glob('*.json'))) == 1


@pytest.mark.asyncio
async def test_expired_avatar_still_served_from_disk_until_channel_visit(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'AVATAR_CACHE_DIR', str(tmp_path))
    cache = AvatarCache()
    monkeypatch.setattr(cache, '_schedule_image', lambda *args, **kwargs: None)
    cache.store_from_channel_info('UCsample2', [{'url': 'https://example.com/old.jpg'}])
    meta = next(tmp_path.glob('*.json'))
    d = json.loads(meta.read_text())
    d['cached_at'] -= 18 * 86400
    d['last_attempt_at'] -= 18 * 86400
    meta.write_text(json.dumps(d))
    second = AvatarCache()
    assert second._should_fetch('UCsample2')
    assert (await second.get('UCsample2'))[0]['url'].endswith('old.jpg')
    assert second._scheduled == set()


@pytest.mark.asyncio
async def test_relay_seeking_206_recovers_from_403_by_smaller_range(monkeypatch):
    content = bytes(i % 256 for i in range(1_400_000))
    requests = []
    def fake_upstream(request: httpx.Request):
        rng = request.headers['range']
        a,b = (int(v) for v in rng[6:].split('-'))
        requests.append((a,b))
        if b-a+1 > 128*1024 and rng != 'bytes=0-0':
            return httpx.Response(403, request=request)
        if a >= len(content):
            return httpx.Response(416, headers={'content-range': f'bytes */{len(content)}'}, request=request)
        b = min(b, len(content)-1)
        return httpx.Response(206, headers={
            'Content-Range':f'bytes {a}-{b}/{len(content)}',
            'Content-Type':'video/mp4',
            'Content-Length':str(b-a+1),
        }, stream=httpx.ByteStream(content[a:b+1]), request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake_upstream))
    # This endpoint accepts only URLs signed by the server. Stub auth solely for
    # the local byte-range unit test, not in production code.
    monkeypatch.setattr(_relay, '_verify', lambda *args: True)
    monkeypatch.setattr(_relay, 'is_safe_url', lambda url: True)
    monkeypatch.setattr(_relay, 'is_youtube_url', lambda url: False)
    import types
    wrapper = types.SimpleNamespace(**{name:getattr(httpx,name) for name in
        ('HTTPStatusError','AsyncHTTPTransport','Timeout','RequestError','RemoteProtocolError',
         'ReadError','WriteError','ConnectError','ReadTimeout')})
    wrapper.AsyncClient = lambda **kw: client
    monkeypatch.setattr(_relay, 'httpx', wrapper)

    request=Request({'type':'http','method':'GET','path':'/proxy/relay',
        'query_string':b'', 'headers':[(b'range', b'bytes=500000-')],
        'scheme':'http','server':('example.org',80)})
    response = await _relay.relay(request,'https://example.org/media','test',int(time.time())+1000)
    data = b''.join([chunk async for chunk in response.body_iterator])
    assert response.status_code == 206
    assert response.headers['content-range'] == f'bytes 500000-{len(content)-1}/{len(content)}'
    assert int(response.headers['content-length']) == len(data)
    assert data == content[500000:]
    assert any(b-a+1 > 128*1024 for a,b in requests)
    assert any(0 < b-a+1 <= 128*1024 and a >= 500000 for a,b in requests)


@pytest.mark.asyncio
async def test_416_for_seek_past_eof(monkeypatch):
    data = b'abc'
    def fake_upstream(request):
        return httpx.Response(206,headers={'Content-Range':'bytes 0-0/3','Content-Type':'video/mp4'},content=b'a',request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake_upstream))
    monkeypatch.setattr(_relay, '_verify', lambda *args: True)
    monkeypatch.setattr(_relay, 'is_safe_url', lambda url: True)
    monkeypatch.setattr(_relay, 'is_youtube_url', lambda url: False)
    import types
    wrapper = types.SimpleNamespace(**{name:getattr(httpx,name) for name in
        ('HTTPStatusError','AsyncHTTPTransport','Timeout','RequestError','RemoteProtocolError',
         'ReadError','WriteError','ConnectError','ReadTimeout')})
    wrapper.AsyncClient = lambda **kw: client
    monkeypatch.setattr(_relay,'httpx',wrapper)
    req=Request({'type':'http','method':'GET','headers':[(b'range',b'bytes=99-')],
                 'path':'/proxy/relay','query_string':b'', 'scheme':'http', 'server':('localhost',80)})
    response=await _relay.relay(req,'https://example.org/media','sig',int(time.time())+1000)
    assert response.status_code == 416
    assert response.headers['content-range'] == 'bytes */3'
