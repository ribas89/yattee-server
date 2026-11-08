"""Regression: yt-dlp DRC audio formats must carry signed refresh metadata."""

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.requests import Request

from converters._formats import convert_formats
from routers.proxy import _relay
import ytdlp_wrapper._youtube as youtube

VIDEO_ID = 'wh6dlpO-f3A'
FMT = '140-drc'
TOTAL = 3 * 1024 * 1024 + 501
DATA = bytes(range(251)) * (TOTAL // 251 + 1)
DATA = DATA[:TOTAL]
OLD = f'https://rr1.googlevideo.com/videoplayback?clen={TOTAL}&itag=140&xtags=drc%3D1&ip=104.28.207.235'
NEW = f'https://rr1.googlevideo.com/videoplayback?clen={TOTAL}&itag=140&xtags=drc%3D1&ip=104.28.207.237'


def _format():
    return {
        'format_id': FMT,
        'url': OLD,
        'ext': 'm4a',
        'vcodec': 'none',
        'acodec': 'mp4a.40.2',
        'tbr': 129,
    }


def _relay_params():
    _, adaptive = convert_formats(
        [_format()], video_id=VIDEO_ID, proxy_mode='relay',
        proxy_base_url='http://localhost/proxy', base_url='http://localhost',
    )
    assert len(adaptive) == 1
    return {k: v[0] for k, v in parse_qs(urlsplit(adaptive[0].url).query).items()}


def _req(range_header):
    return Request({
        'type': 'http', 'method': 'GET', 'headers': [(b'range', range_header.encode())],
        'path': '/proxy/relay', 'query_string': b'', 'scheme': 'http',
        'server': ('localhost', 80),
    })


def test_drc_audio_gets_signed_refresh_metadata(monkeypatch):
    monkeypatch.setattr(_relay.token_utils, '_get_signing_key', lambda: b'test-secret-for-regression')
    params = _relay_params()
    assert params['video_id'] == VIDEO_ID
    assert params['itag'] == FMT
    assert _relay._verify(params['url'], int(params['exp']), params['sig'], VIDEO_ID, FMT)


def test_drc_audio_signature_rejects_variant_substitution(monkeypatch):
    monkeypatch.setattr(_relay.token_utils, '_get_signing_key', lambda: b'test-secret-for-regression')
    params = _relay_params()
    assert not _relay._verify(params['url'], int(params['exp']), params['sig'], VIDEO_ID, '140')


@pytest.mark.asyncio
async def test_drc_audio_refreshes_after_first_mib_then_resumes(monkeypatch):
    def upstream(request):
        start, end = map(int, request.headers['range'][6:].split('-'))
        if request.url.params.get('ip') == '104.28.207.235' and start >= 1048576:
            return httpx.Response(403, request=request)
        if start >= TOTAL:
            return httpx.Response(416, headers={'Content-Range': f'bytes */{TOTAL}'}, request=request)
        end = min(end, TOTAL - 1)
        return httpx.Response(206, request=request, headers={
            'Content-Range': f'bytes {start}-{end}/{TOTAL}',
            'Content-Length': str(end - start + 1),
            'Content-Type': 'audio/mp4',
        }, stream=httpx.ByteStream(DATA[start:end+1]))

    transport = httpx.MockTransport(upstream)
    client = httpx.AsyncClient(transport=transport)
    mock = SimpleNamespace(**{name: getattr(httpx, name) for name in
        ('HTTPStatusError', 'AsyncHTTPTransport', 'Timeout', 'RequestError',
         'RemoteProtocolError', 'ReadError', 'WriteError', 'ConnectError', 'ReadTimeout')})
    mock.AsyncClient = lambda **kw: client
    monkeypatch.setattr(_relay, 'httpx', mock)
    monkeypatch.setattr(_relay, 'CHUNK_SIZE', 1048576)
    monkeypatch.setattr(_relay, 'is_safe_url', lambda _url: True)
    monkeypatch.setattr(_relay, 'get_settings', lambda: SimpleNamespace(
        effective_yt_egress_proxy=lambda: None,
        effective_ip_family=lambda: 'auto',
    ))
    monkeypatch.setattr(_relay.token_utils, '_get_signing_key', lambda: b'test-secret-for-regression')

    called = []
    async def fresh(video_id, use_cache=True):
        called.append((video_id, use_cache))
        return {'formats': [
            {'format_id': '140', 'url': NEW.replace('xtags=drc%3D1', 'xtags=none')},
            {'format_id': FMT, 'url': NEW},
        ]}

    monkeypatch.setattr(youtube, 'get_video_info', fresh)
    params = _relay_params()
    params['exp'] = int(params['exp'])
    response = await _relay.relay(_req('bytes=0-'), **params)
    received = b''.join([piece async for piece in response.body_iterator])
    assert response.status_code == 206
    assert int(response.headers['content-length']) == TOTAL
    assert received == DATA
    assert called == [(VIDEO_ID, False)]
    await client.aclose()
