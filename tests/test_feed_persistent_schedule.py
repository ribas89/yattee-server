"""Regression tests for persistent per-channel feed refresh intervals."""

import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import database.repositories.feed as feed_repository
import feed_fetcher


def test_feed_status_join_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "yattee.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE watched_channels (
        channel_id TEXT NOT NULL, site TEXT NOT NULL, channel_name TEXT,
        channel_url TEXT, avatar_url TEXT, last_requested TEXT,
        PRIMARY KEY(channel_id, site)
    );
    CREATE TABLE feed_fetch_status (
        channel_id TEXT NOT NULL, site TEXT NOT NULL, last_fetch TEXT,
        fetch_error TEXT, max_videos_fetched INTEGER, pagination_limited BOOLEAN,
        pagination_limit_reason TEXT, PRIMARY KEY(channel_id, site)
    );
    INSERT INTO watched_channels (channel_id,site) VALUES ('UCtest','youtube');
    """)
    conn.commit()
    conn.close()

    @contextmanager
    def connection():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(feed_repository, "get_connection", connection)
    now = datetime.now(UTC)
    feed_repository.update_fetch_status('UCtest', 'youtube', success=True)
    # A failure shouldn't make an already successful fetch look newer.
    before = feed_repository.get_all_watched_channels()[0]['last_fetch']
    assert datetime.fromisoformat(before) >= now
    feed_repository.update_fetch_status('UCtest', 'youtube', success=False, error='403')
    after = feed_repository.get_all_watched_channels()[0]
    assert after['last_fetch'] == before
    assert after['fetch_error'] == '403'
    # Read from disk through a new connection: restart-safe.
    assert feed_repository.get_all_watched_channels()[0]['last_fetch'] == before


def test_recent_feed_skips_network_calls(monkeypatch):
    now = datetime.now(UTC)
    channel = {
        'channel_id': 'UCtest', 'site': 'youtube', 'channel_url': '',
        'last_fetch': (now - timedelta(minutes=10)).isoformat()
    }
    monkeypatch.setattr(feed_fetcher.database, 'get_all_watched_channels', lambda: [channel])
    monkeypatch.setattr(feed_fetcher.config, 'FEED_REFRESH_MINUTES', 360)

    async def never_fetch(*args, **kwargs):
        raise AssertionError('fresh channel made an unnecessary YouTube request')

    monkeypatch.setattr(feed_fetcher, 'fetch_channel_feed', never_fetch)
    asyncio.run(feed_fetcher.fetch_all_channels())


def test_expired_and_timezone_timestamps():
    now = datetime.now(UTC)
    assert feed_fetcher._is_recent_feed((now - timedelta(minutes=30)).isoformat(), 3600, now)
    assert not feed_fetcher._is_recent_feed((now - timedelta(minutes=90)).isoformat(), 3600, now)
    assert not feed_fetcher._is_recent_feed(None, 3600, now)
    assert feed_fetcher._is_recent_feed((now - timedelta(minutes=30)).replace(tzinfo=None).isoformat(), 3600, now)
    assert not feed_fetcher._is_recent_feed('bad-timestamp', 3600, now)
    assert not feed_fetcher._is_recent_feed((now + timedelta(hours=24)).isoformat(), 3600, now)


def test_env_interval(monkeypatch):
    monkeypatch.setattr(feed_fetcher.config, 'FEED_REFRESH_MINUTES', 120)
    assert feed_fetcher._feed_interval_seconds() == 7200
