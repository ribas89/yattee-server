"""Mid-stream and seek recovery when a signed GoogleVideo URL stops working."""
import asyncio
import time
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from routers.proxy import _relay
import ytdlp_wrapper._youtube as youtube

TOTAL = 400_001
DATA = bytes(i % 251 for i in range(TOTAL))
OLD = f'https://rr1.googlevideo.com/videoplayback?clen={TOTAL}&itag=134&ip=104.28.207.235'
NEW = f'https://rr1.googlevideo.com/videoplayback?clen={TOTAL}&itag=134&ip=104.28.207.237'


def upstream(request: httpx.Request):
    start, end = map(int, request.headers['range'][6:].split('-'))
    if request.url.params.get('ip') == '104.28.207.235' and start >= 131072:
        return httpx.Response(403, request=request)
    if start >= TOTAL:
        return httpx.Response(416, headers={'Content-Range': f'bytes */{TOTAL}'}, request=request)
    end = min(end, TOTAL - 1)
    return httpx.Response(206, request=request, headers={
        'Content-Range': f'bytes {start}-{end}/{TOTAL}',
        'Content-Length': str(end-start+1),
        'Content-Type': 'video/mp4',
    }, stream=httpx.ByteStream(DATA[start:end+1]))


@pytest.fixture
def mock_network(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    mock = SimpleNamespace(**{name: getattr(httpx, name) for name in
        ('HTTPStatusError', 'AsyncHTTPTransport', 'Timeout', 'RequestError',
         'RemoteProtocolError', 'ReadError', 'WriteError', 'ConnectError', 'ReadTimeout')})
    mock.AsyncClient = lambda **kw: client
    monkeypatch.setattr(_relay, 'httpx', mock)
    monkeypatch.setattr(_relay, 'CHUNK_SIZE', 131072)
    monkeypatch.setattr(_relay, 'is_safe_url', lambda _url: True)
    monkeypatch.setattr(_relay, 'get_settings', lambda: SimpleNamespace(effective_yt_egress_proxy=lambda: None, effective_ip_family=lambda: 'auto'))
    monkeypatch.setattr(_relay.token_utils, '_get_signing_key', lambda: b'mock-key-123456789')
    async def fresh(video_id, use_cache=True):
        assert video_id == 'wh6dlpO-f3A'
        assert use_cache is False
        return {'formats': [{'format_id': '134', 'url': NEW}]}
    monkeypatch.setattr(youtube, 'get_video_info', fresh)
    return client


def req(rng):
    return Request({'type': 'http', 'method': 'GET', 'headers': [(b'range', rng.encode())],
                    'path': '/proxy/relay', 'query_string': b'', 'scheme': 'http',
                    'server': ('localhost', 80)})


def params():
    link = _relay.signed_relay_url('http://localhost', OLD, video_id='wh6dlpO-f3A', itag='134')
    parsed = parse_qs(urlsplit(link).query)
    p = {k: v[0] for k, v in parsed.items()}
    p["exp"] = int(p["exp"])
    return p


@pytest.mark.asyncio
async def test_recover_from_403_midstream(mock_network):
    response = await _relay.relay(req('bytes=0-'), **params())
    assert response.status_code == 206
    received = b''.join([piece async for piece in response.body_iterator])
    assert received == DATA
    assert int(response.headers['content-length']) == TOTAL


@pytest.mark.asyncio
async def test_recover_from_403_at_seek_offset(mock_network):
    response = await _relay.relay(req('bytes=200000-'), **params())
    assert response.status_code == 206
    received = b''.join([piece async for piece in response.body_iterator])
    assert received == DATA[200000:]
    assert response.headers['content-range'] == f'bytes 200000-{TOTAL-1}/{TOTAL}'


@pytest.mark.asyncio
async def test_signature_binds_video_and_itag(mock_network):
    p = params()
    p['video_id'] = 'jNQXAC9IVRw'
    with pytest.raises(HTTPException) as e:
        await _relay.relay(req('bytes=0-'), **p)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_refuse_wrong_byte_length(monkeypatch, mock_network):
    async def wrong(video_id, use_cache=True):
        return {'formats': [{'format_id': '134', 'url': NEW.replace(f'clen={TOTAL}', 'clen=999999')} ]}
    monkeypatch.setattr(youtube, 'get_video_info', wrong)
    with pytest.raises(HTTPException) as e:
        await _relay.relay(req('bytes=200000-'), **params())
    assert e.value.status_code == 502


def test_ytdlp_converter_embeds_signed_refresh_metadata(monkeypatch):
    from converters._formats import convert_formats
    formats = [{'format_id': '134', 'url': OLD, 'ext': 'mp4', 'height': 360,
                'vcodec': 'avc1.4d401e', 'acodec': 'none'}]
    _, adaptive = convert_formats(
        formats, video_id='wh6dlpO-f3A', proxy_base_url='http://localhost/proxy',
        base_url='http://localhost', proxy_mode='relay',
    )
    p = parse_qs(urlsplit(adaptive[0].url).query)
    assert p['video_id'] == ['wh6dlpO-f3A']
    assert p['itag'] == ['134']
    assert _relay._verify(p['url'][0], int(p['exp'][0]), p['sig'][0], p['video_id'][0], p['itag'][0])


def test_unsigned_metadata_cannot_be_attached_to_old_relay_url():
    old = _relay.signed_relay_url('http://localhost', OLD)
    p = parse_qs(urlsplit(old).query)
    assert 'video_id' not in p
    assert _relay._verify(p['url'][0], int(p['exp'][0]), p['sig'][0])
    assert not _relay._verify(p['url'][0], int(p['exp'][0]), p['sig'][0], 'wh6dlpO-f3A', '134')
