"""Regression tests for file-backed description and comment caching."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import config
import metadata_cache
from routers import comments


@pytest.fixture
def cache_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'VIDEO_DESCRIPTION_CACHE_DIR', str(tmp_path / 'desc'))
    monkeypatch.setattr(config, 'COMMENTS_CACHE_DIR', str(tmp_path / 'comments'))
    monkeypatch.setattr(config, 'COMMENTS_CACHE_MINUTES', 360)
    return tmp_path


def test_description_persists_without_expiry(cache_dirs):
    video_id = 'wh6dlpO-f3A'
    first = SimpleNamespace(description='Full original description', descriptionHtml='<p>Original</p>')
    metadata_cache.apply_description(video_id, first)
    assert list((cache_dirs/'desc').glob('*.json'))
    later = SimpleNamespace(description='Newer content', descriptionHtml='Newer HTML')
    metadata_cache.apply_description(video_id, later)
    assert later.description == 'Full original description'
    assert later.descriptionHtml == '<p>Original</p>'
    assert 'http_headers' not in (cache_dirs/'desc').glob('*.json').__iter__().__next__().read_text()


def test_comments_cache_survives_repeated_reads_and_separates_pages(cache_dirs):
    video_id = 'wh6dlpO-f3A'
    page1 = {'comments': [{'commentId': 'abc'}], 'continuation': 'page2'}
    metadata_cache.store_comments(video_id, None, page1)
    assert metadata_cache.get_comments(video_id, None) == page1
    assert metadata_cache.get_comments(video_id, 'page2') is None
    metadata_cache.store_comments(video_id, 'page2', {'comments': [{'commentId':'xyz'}], 'continuation': None})
    assert metadata_cache.get_comments(video_id, 'page2')['comments'][0]['commentId'] == 'xyz'
    assert len(list((cache_dirs/'comments').glob('*.json'))) == 2


def test_comment_ttl_and_disabled_cache(cache_dirs, monkeypatch):
    video_id = 'wh6dlpO-f3A'
    data = {'comments': [{'commentId': 'abc'}], 'continuation': None}
    metadata_cache.store_comments(video_id, None, data)
    path = next((cache_dirs/'comments').glob('*.json'))
    record = json.loads(path.read_text())
    record['fetched_at'] = (datetime.now(timezone.utc) - timedelta(minutes=361)).isoformat()
    path.write_text(json.dumps(record))
    assert metadata_cache.get_comments(video_id, None) is None
    monkeypatch.setattr(config, 'COMMENTS_CACHE_MINUTES', 0)
    assert metadata_cache.get_comments(video_id, None) is None
    metadata_cache.store_comments('a-b-c123456', None, data)
    assert len(list((cache_dirs/'comments').glob('*.json'))) == 1


@pytest.mark.asyncio
async def test_comments_endpoint_only_fetches_first_time(cache_dirs, monkeypatch):
    payload = {'comments': [{'commentId': 'abc'}], 'continuation': None}
    fetch = AsyncMock(return_value=payload)
    monkeypatch.setattr(comments.innertube, 'get_comments', fetch)
    one = await comments.get_comments('wh6dlpO-f3A', None)
    two = await comments.get_comments('wh6dlpO-f3A', None)
    assert one == two == payload
    assert fetch.await_count == 1

@pytest.mark.asyncio
async def test_empty_inner_tube_comments_are_cached(cache_dirs, monkeypatch):
    fetch = AsyncMock(return_value={'comments': [], 'continuation': None})
    monkeypatch.setattr(comments.innertube, 'get_comments', fetch)
    monkeypatch.setattr(comments.invidious_proxy, 'is_enabled', lambda: False)
    one = await comments.get_comments('wh6dlpO-f3A', None)
    two = await comments.get_comments('wh6dlpO-f3A', None)
    assert one == two == {'comments': [], 'continuation': None}
    assert fetch.await_count == 1
