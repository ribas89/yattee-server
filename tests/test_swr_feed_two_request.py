"""Feed API first request cache-only, second request conditional SWR.

All tests use fake database IO and never make upstream network calls.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import feed_fetcher
from routers import subscriptions


@pytest.fixture

def feed_state(monkeypatch):
    subscriptions._seen_feed_groups.clear()
    subscriptions._initial_jobs.clear()
    feed_fetcher._due_jobs.clear()
    feed_fetcher._due_last_attempt.clear()
    monkeypatch.setattr(feed_fetcher.config, 'FEED_REFRESH_MINUTES', 360)
    store = {
        ('UC111111111', 'youtube'): {
            'channel_id': 'UC111111111', 'site': 'youtube', 'channel_url': None,
            'last_fetch': (datetime.now(UTC) - timedelta(minutes=361)).isoformat(),
        },
        ('UC222222222', 'youtube'): {
            'channel_id': 'UC222222222', 'site': 'youtube', 'channel_url': None,
            'last_fetch': (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
        },
    }
    monkeypatch.setattr(feed_fetcher.database, 'get_all_watched_channels', lambda: list(store.values()))
    monkeypatch.setattr(subscriptions.database, 'upsert_watched_channels', lambda x: None)
    monkeypatch.setattr(subscriptions.database, 'get_cached_channel_ids',
                        lambda x: {(c['channel_id'], c['site']) for c in x})
    monkeypatch.setattr(subscriptions.database, 'get_errored_channel_ids', lambda x: set())
    monkeypatch.setattr(subscriptions.database, 'get_feed_for_channels', lambda *a, **kw: [])
    monkeypatch.setattr(subscriptions.database, 'get_feed_count_for_channels', lambda *a, **kw: 0)
    async def complete(channel_id, site, channel_url):
        await asyncio.sleep(0)
        store[(channel_id, site)]['last_fetch'] = datetime.now(UTC).isoformat()
    fetch = AsyncMock(side_effect=complete)
    monkeypatch.setattr(feed_fetcher, 'fetch_single_channel', fetch)
    yield store, fetch
    subscriptions._seen_feed_groups.clear()
    feed_fetcher._due_last_attempt.clear()


def req(offset=0):
    return subscriptions.StatelessFeedRequest(
        channels=[subscriptions.ChannelFeedRequest(channel_id=key, site='youtube')
                  for key in ('UC111111111', 'UC222222222')],
        offset=offset,
    )


@pytest.mark.asyncio
async def test_first_cached_second_only_stale_then_no_duplicate(feed_state):
    _, fetch = feed_state
    first = await subscriptions.post_feed(req())
    assert first.status == 'ready' and first.refreshQueued == 0
    await asyncio.sleep(0)
    fetch.assert_not_awaited()
    second = await subscriptions.post_feed(req())
    assert second.refreshQueued == 1
    # The refreshing task is separate from the response, so it does not block it.
    await asyncio.sleep(0.01)
    fetch.assert_awaited_once_with('UC111111111', 'youtube', None)
    third = await subscriptions.post_feed(req())
    assert third.refreshQueued == 0
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_pagination_does_not_count_as_second_load(feed_state):
    _, fetch = feed_state
    await subscriptions.post_feed(req(offset=50))
    first = await subscriptions.post_feed(req(offset=0))
    assert first.refreshQueued == 0
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_interval_not_overridden_on_second_load(feed_state):
    store, fetch = feed_state
    for value in store.values():
        value['last_fetch'] = (datetime.now(UTC) - timedelta(minutes=359)).isoformat()
    await subscriptions.post_feed(req())
    second = await subscriptions.post_feed(req())
    assert second.refreshQueued == 0
    await asyncio.sleep(0)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_due_refresh_has_backoff(feed_state, monkeypatch):
    _, fetch = feed_state
    fetch.side_effect = RuntimeError('network down')
    await subscriptions.post_feed(req())
    second = await subscriptions.post_feed(req())
    assert second.refreshQueued == 1
    await asyncio.sleep(0.01)
    third = await subscriptions.post_feed(req())
    assert third.refreshQueued == 0
    assert fetch.await_count == 1
