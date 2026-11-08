"""Cached full-media video details are returned without a YouTube call."""
import asyncio
import sys
import types
# The container used for source testing lacks bcrypt; authentication itself
# is not involved in these mocked video-route tests. Do not ship this stub
# outside tests or use it to validate password handling.
try:
    import bcrypt  # noqa: F401
except ModuleNotFoundError:
    sys.modules.setdefault('bcrypt', types.ModuleType('bcrypt'))
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from starlette.requests import Request

import config
import tokens
import video_cache
import video_info_cache
from models import FormatStream, VideoResponse
from routers import videos

VID = 'LEdN7vPkpdU'


def response(url):
    return VideoResponse(
        videoId=VID, title='Cache-ready video', description='Description!',
        author='Someone', authorId='UC123', lengthSeconds=727,
        formatStreams=[FormatStream(
            url=url, itag='18', type='video/mp4', quality='360p', container='mp4', height=360,
        )],
        recommendedVideos=[],
    )


@pytest.fixture
def cached_video(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'VIDEO_CACHE_DIR', str(tmp_path / 'videos'))
    monkeypatch.setattr(config, 'VIDEO_CACHE_DAYS', 30)
    monkeypatch.setattr(config, 'VIDEO_INFO_CACHE_DIR', str(tmp_path / 'video-info'))
    monkeypatch.setattr(config, 'VIDEO_DESCRIPTION_CACHE_DIR', str(tmp_path / 'descriptions'))
    monkeypatch.setattr(tokens, '_signing_secret', 'test-token-key-stable')
    writer = video_cache.begin(VID, '18', 8, 'video/mp4', 0)
    assert writer
    writer.write(b'ABCDEFGH')
    writer.close()
    assert video_cache.lookup(VID, '18') is not None
    assert video_info_cache.store(VID, response('https://expired.youtube.media/file'))
    yield


def test_cached_video_mints_new_playable_local_url(cached_video):
    data = video_info_cache.load(VID, 'https://my.yattee.local')
    assert data is not None
    assert data.description == 'Description!'
    assert data.title == 'Cache-ready video'
    link = data.formatStreams[0].url
    assert urlparse(link).path == '/proxy/cached/LEdN7vPkpdU/18'
    assert 'expired.youtube.media' not in link
    assert 'exp=' in link and 'sig=' in link


@pytest.mark.asyncio
async def test_video_api_cached_path_skips_all_extraction(cached_video, monkeypatch):
    monkeypatch.setattr(videos, 'validate_extractor_allowed', lambda extractor: None)
    monkeypatch.setattr(videos, 'get_settings', lambda: SimpleNamespace(invidious_proxy_videos=False))
    monkeypatch.setattr(videos, 'get_current_user_from_request', lambda request: None)
    monkeypatch.setattr(videos.database, 'get_site_by_extractor',
                        lambda extractor: {'proxy_streaming': True})
    monkeypatch.setattr(videos.invidious_proxy, 'is_enabled', lambda: False)
    monkeypatch.setattr(videos.video_info_cache, 'due', lambda video_id: False)
    extractor = AsyncMock(side_effect=AssertionError('metadata from network should not load'))
    monkeypatch.setattr(videos, 'get_video_info', extractor)
    request = Request({'type': 'http', 'scheme': 'https', 'method': 'GET',
                       'path': '/api/v1/videos/'+VID, 'headers': [],
                       'server': ('my.yattee.local', 443), 'query_string': b''})
    result = await videos.get_video(VID, request, proxy=None, proxy_mode=None,
                                    refresh=False, invidious=None)
    assert result.title == 'Cache-ready video'
    assert result.formatStreams[0].url.startswith('https://my.yattee.local/proxy/cached/')
    extractor.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_visit_defers_recommendations(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'VIDEO_INFO_CACHE_DIR', str(tmp_path / 'infos'))
    monkeypatch.setattr(config, 'VIDEO_DESCRIPTION_CACHE_DIR', str(tmp_path / 'descs'))
    monkeypatch.setattr(videos, 'validate_extractor_allowed', lambda extractor: None)
    monkeypatch.setattr(videos, 'get_settings', lambda: SimpleNamespace(invidious_proxy_videos=False))
    monkeypatch.setattr(videos, 'get_current_user_from_request', lambda request: None)
    monkeypatch.setattr(videos.database, 'get_site_by_extractor', lambda extractor: {'proxy_streaming': True})
    monkeypatch.setattr(videos.invidious_proxy, 'is_enabled', lambda: False)
    monkeypatch.setattr(videos, 'get_video_info', AsyncMock(return_value={'id': VID}))
    monkeypatch.setattr(videos, 'ytdlp_to_video_response', lambda *a, **k: response(
        'https://my.yattee.local/proxy/relay?url=thing&exp=123&sig=abc'))
    called = asyncio.Event()
    release = asyncio.Event()

    async def delayed_recommendations(_):
        called.set()
        await release.wait()
        return {'recommendedVideos': []}

    monkeypatch.setattr(videos.innertube, 'is_enabled', lambda: True)
    monkeypatch.setattr(videos.innertube, 'get_video_player_next', delayed_recommendations)
    request = Request({'type': 'http', 'scheme': 'https', 'method': 'GET',
                       'path': '/api/v1/videos/'+VID, 'headers': [],
                       'server': ('my.yattee.local', 443), 'query_string': b''})
    result = await asyncio.wait_for(
        videos.get_video(VID, request, proxy=None, proxy_mode=None,
                         refresh=False, invidious=None), timeout=1.0)
    assert result.title == 'Cache-ready video'  # Network /next hasn't completed
    await asyncio.wait_for(called.wait(), 1.0)
    release.set()
    await asyncio.sleep(0.05)
    assert video_info_cache._read_record(VID) is not None
