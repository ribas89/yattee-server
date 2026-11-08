"""Request parity regression: signed yt-dlp headers, randomized HTTP ranges, and refresh."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from converters._formats import convert_formats
from routers.proxy import _relay
import ytdlp_wrapper._youtube as youtube

VIDEO = 'wh6dlpO-f3A'
TOTAL = 2_600_000
DATA = bytes(range(251)) * (TOTAL // 251 + 1)
DATA = DATA[:TOTAL]
OLD = f'https://rr4.googlevideo.com/videoplayback?itag=140&clen={TOTAL}&stage=old'
NEW = f'https://rr4.googlevideo.com/videoplayback?itag=140&clen={TOTAL}&stage=new'


def params():
    _, adaptive = convert_formats([{
        'url': OLD, 'format_id': '140-drc', 'ext': 'm4a',
        'acodec': 'mp4a.40.2', 'vcodec': 'none',
        'http_headers': {
            'User-Agent': 'yt-dlp-working-client',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
            'Authorization': 'do-not-leak',
            'Cookie': 'private-cookie',
        },
    }], video_id=VIDEO, proxy_mode='relay',
        proxy_base_url='http://localhost/proxy', base_url='http://localhost')
    result = {k: v[0] for k, v in parse_qs(urlsplit(adaptive[0].url).query).items()}
    result['exp'] = int(result['exp'])
    return result


def client_request(header='bytes=0-'):
    return Request({
        'type': 'http', 'method': 'GET',
        'headers': [(b'range', header.encode())],
        'path': '/proxy/relay', 'query_string': b'',
        'scheme': 'http', 'server': ('localhost', 80),
    })


def configure(monkeypatch, upstream):
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    mock = SimpleNamespace(**{name: getattr(httpx, name) for name in
        ('HTTPStatusError', 'AsyncHTTPTransport', 'Timeout', 'RequestError',
         'RemoteProtocolError', 'ReadError', 'WriteError', 'ConnectError', 'ReadTimeout')})
    mock.AsyncClient = lambda **kw: client
    monkeypatch.setattr(_relay, 'httpx', mock)
    monkeypatch.setattr(_relay, 'CHUNK_SIZE', 1048576)
    monkeypatch.setattr(_relay.random, 'randint', lambda a, b: a)
    monkeypatch.setattr(_relay, 'is_safe_url', lambda _url: True)
    monkeypatch.setattr(_relay, 'get_settings', lambda: SimpleNamespace(
        effective_yt_egress_proxy=lambda: None,
        effective_ip_family=lambda: 'auto',
    ))
    monkeypatch.setattr(_relay.token_utils, '_get_signing_key', lambda: b'header-test-secret')
    return client


def test_signed_header_parameter_is_non_sensitive_and_authenticated(monkeypatch):
    monkeypatch.setattr(_relay.token_utils, '_get_signing_key', lambda: b'header-test-secret')
    p = params()
    assert 'rh' in p
    rh = _relay._decode_download_headers(p['rh'])
    assert rh['User-Agent'] == 'yt-dlp-working-client'
    assert rh['Accept-Language'] == 'en-us,en;q=0.5'
    assert 'Cookie' not in rh
    assert 'Authorization' not in rh
    assert _relay._verify(p['url'], p['exp'], p['sig'], VIDEO, '140-drc', p['rh'])
    assert not _relay._verify(p['url'], p['exp'], p['sig'], VIDEO, '140-drc')


@pytest.mark.asyncio
async def test_relay_uses_format_headers_and_varied_chunk_sizes(monkeypatch):
    ranges = []

    def upstream(req):
        if req.headers.get('User-Agent') != 'yt-dlp-working-client':
            return httpx.Response(403, request=req)
        if req.headers.get('Accept-Language') != 'en-us,en;q=0.5':
            return httpx.Response(403, request=req)
        start, end = map(int, req.headers['range'][6:].split('-'))
        if end - start + 1 > int(1048576 * .95):
            return httpx.Response(403, request=req)
        ranges.append((start, end))
        end = min(end, TOTAL - 1)
        return httpx.Response(206, request=req,
            headers={'Content-Range': f'bytes {start}-{end}/{TOTAL}',
                     'Content-Length': str(end-start+1), 'Content-Type': 'audio/mp4'},
            stream=httpx.ByteStream(DATA[start:end+1]))

    client = configure(monkeypatch, upstream)
    response = await _relay.relay(client_request(), **params())
    received = b''.join([piece async for piece in response.body_iterator])
    assert received == DATA
    assert len(ranges) >= 4
    assert ranges[0] == (0, 0)
    assert ranges[1][1] - ranges[1][0] + 1 == int(1048576 * .95)
    await client.aclose()


@pytest.mark.asyncio
async def test_403_refresh_updates_headers_from_reextracted_format(monkeypatch):
    visited_new = []

    def upstream(req):
        stage = req.url.params.get('stage')
        ua = req.headers.get('User-Agent')
        start, end = map(int, req.headers['Range'][6:].split('-'))
        if stage == 'old':
            if ua != 'yt-dlp-working-client' or start >= 1048576:
                return httpx.Response(403, request=req)
        else:
            if ua != 'new-yt-dlp-working-client':
                return httpx.Response(403, request=req)
            visited_new.append(start)
        end = min(end, TOTAL - 1)
        return httpx.Response(206, request=req,
            headers={'Content-Range': f'bytes {start}-{end}/{TOTAL}',
                     'Content-Length': str(end-start+1)},
            stream=httpx.ByteStream(DATA[start:end+1]))

    client = configure(monkeypatch, upstream)

    async def fresh(video_id, use_cache=True):
        assert video_id == VIDEO and not use_cache
        return {'formats': [{
            'format_id': '140-drc', 'url': NEW,
            'http_headers': {'User-Agent': 'new-yt-dlp-working-client'},
        }]}

    monkeypatch.setattr(youtube, 'get_video_info', fresh)
    response = await _relay.relay(client_request(), **params())
    received = b''.join([piece async for piece in response.body_iterator])
    assert received == DATA
    assert visited_new
    await client.aclose()


@pytest.mark.asyncio
async def test_modified_signed_header_parameter_is_rejected(monkeypatch):
    client = configure(monkeypatch, lambda req: httpx.Response(200, request=req))
    p = params()
    p['rh'] += 'A'
    with pytest.raises(HTTPException) as err:
        await _relay.relay(client_request(), **p)
    assert err.value.status_code == 403
    await client.aclose()
