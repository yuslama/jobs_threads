from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import scraper as scraper_module
from src.parser import Post
from src.scraper import NotAvailable, RateLimited, ScrapeError
from src.search_api import QuotaExhausted, SearchError, SearchProvider
from src.sources import profiles as profiles_module
from src.sources import search as search_module
from src.sources.profiles import ProfileSource
from src.sources.search import SearchSource
from src.store import Store


def make_post(pk: str, username: str, caption: str = "Loker", hours_old: float = 1.0) -> Post:
    return Post(pk=pk, code="C" + pk, username=username, caption=caption,
                taken_at=datetime.now(timezone.utc) - timedelta(hours=hours_old))


class FakeScraper:
    """Stands in for the Playwright scraper. profiles/posts map inputs to results."""

    def __init__(self, profiles=None, posts=None):
        self.profiles = profiles or {}
        self.posts = posts or {}
        self.profile_calls: list[str] = []
        self.post_calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def fetch_profile(self, username, limit=None):
        self.profile_calls.append(username)
        outcome = self.profiles.get(username, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome[:limit] if limit else outcome

    async def fetch_post(self, url):
        self.post_calls.append(url)
        outcome = self.posts.get(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def patched(monkeypatch):
    """Swap the real scraper for a fake in both sources, and skip the delays."""
    holder: dict[str, FakeScraper] = {}

    def install(fake: FakeScraper) -> FakeScraper:
        holder["fake"] = fake
        monkeypatch.setattr(profiles_module, "ThreadsScraper", lambda **kw: fake)
        monkeypatch.setattr(search_module, "ThreadsScraper", lambda **kw: fake)
        return fake

    async def no_delay(bounds):
        return 0.0

    monkeypatch.setattr(profiles_module, "polite_delay", no_delay)
    monkeypatch.setattr(search_module, "polite_delay", no_delay)
    return install


# ------------------------------------------------------------- Source A
@pytest.mark.asyncio
async def test_profile_source_returns_posts(config, store, patched):
    config.source_a.watched_accounts = ["lokerjakarta", "lokerbandung"]
    patched(FakeScraper(profiles={
        "lokerjakarta": [make_post("1", "lokerjakarta")],
        "lokerbandung": [make_post("2", "lokerbandung")],
    }))

    result = await ProfileSource(config, store).collect()

    assert result.ok and [p.pk for p in result.posts] == ["1", "2"]
    assert result.notes["accounts_polled"] == 2


@pytest.mark.asyncio
async def test_a_private_profile_does_not_kill_the_loop(config, store, patched):
    config.source_a.watched_accounts = ["gone", "alive"]
    patched(FakeScraper(profiles={
        "gone": NotAvailable("404"),
        "alive": [make_post("2", "alive")],
    }))

    result = await ProfileSource(config, store).collect()

    assert result.ok
    assert [p.pk for p in result.posts] == ["2"]
    assert result.errors == ["@gone: unavailable"]


@pytest.mark.asyncio
async def test_rate_limiting_aborts_the_cycle_as_a_failure(config, store, patched):
    config.source_a.watched_accounts = ["first", "second"]
    fake = patched(FakeScraper(profiles={"first": RateLimited("403"), "second": []}))

    result = await ProfileSource(config, store).collect()

    assert not result.ok and "rate limited" in result.fatal
    assert fake.profile_calls == ["first"]  # stopped, did not keep digging


@pytest.mark.asyncio
async def test_every_profile_failing_is_a_broken_source(config, store, patched):
    config.source_a.watched_accounts = ["a", "b"]
    patched(FakeScraper(profiles={"a": ScrapeError("boom"), "b": ScrapeError("boom")}))

    result = await ProfileSource(config, store).collect()

    assert not result.ok and "all 2 profiles failed" in result.fatal


@pytest.mark.asyncio
async def test_first_poll_skips_the_backlog_but_later_polls_do_not(config, store, patched):
    config.source_a.watched_accounts = ["lokerjakarta"]
    config.source_a.first_run_max_age_hours = 24
    fake = patched(FakeScraper(profiles={"lokerjakarta": [
        make_post("new", "lokerjakarta", hours_old=2),
        make_post("old", "lokerjakarta", hours_old=100),
    ]}))
    source = ProfileSource(config, store)

    first = await source.collect()
    assert [p.pk for p in first.posts] == ["new"]
    assert store.is_post_seen("old")  # recorded so it can never notify later

    fake.profiles["lokerjakarta"] = [make_post("older", "lokerjakarta", hours_old=200)]
    second = await source.collect()
    assert [p.pk for p in second.posts] == ["older"]  # cutoff only applies once


# ------------------------------------------------------------- Source B
class FakeProvider(SearchProvider):
    name = "fake"

    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.calls: list[str] = []

    async def search(self, query, limit, date_restrict):
        self.calls.append(query)
        if self.error:
            raise self.error
        return list(self.results)


@pytest.mark.asyncio
async def test_search_source_keeps_only_post_urls(config, store, patched):
    url = "https://www.threads.net/@randomdev/post/AAA"
    provider = FakeProvider(results=[
        url,
        "https://www.threads.net/@randomdev",          # profile page
        "https://www.threads.net/tag/loker",           # tag page
        "https://example.com/@x/post/1",               # wrong domain
    ])
    fake = patched(FakeScraper(posts={url: make_post("1", "randomdev")}))

    result = await SearchSource(config, store, provider).collect()

    assert fake.post_calls == [url]
    assert [p.pk for p in result.posts] == ["1"]
    assert result.notes["urls_found"] == 1


@pytest.mark.asyncio
async def test_urls_are_scraped_once_across_cycles(config, store, patched):
    url = "https://www.threads.net/@randomdev/post/AAA"
    provider = FakeProvider(results=[url])
    fake = patched(FakeScraper(posts={url: make_post("1", "randomdev")}))
    source = SearchSource(config, store, provider)

    await source.collect()
    second = await source.collect()

    assert fake.post_calls == [url]
    assert second.notes["urls_new"] == 0


@pytest.mark.asyncio
async def test_dead_post_url_is_never_retried(config, store, patched):
    url = "https://www.threads.net/@gone/post/AAA"
    provider = FakeProvider(results=[url])
    fake = patched(FakeScraper(posts={url: NotAvailable("410")}))
    source = SearchSource(config, store, provider)

    first = await source.collect()
    assert first.ok and first.posts == []
    assert store.is_url_seen(url)

    await source.collect()
    assert fake.post_calls == [url]  # not tried a second time


@pytest.mark.asyncio
async def test_transient_scrape_failure_leaves_the_url_retryable(config, store, patched):
    url = "https://www.threads.net/@randomdev/post/AAA"
    provider = FakeProvider(results=[url])
    fake = patched(FakeScraper(posts={url: ScrapeError("timeout")}))

    result = await SearchSource(config, store, provider).collect()

    assert result.ok and result.errors
    assert not store.is_url_seen(url)


@pytest.mark.asyncio
async def test_quota_exhaustion_is_a_clean_stop_not_an_error(config, store, patched):
    config.source_b.queries = ["q1", "q2", "q3"]
    config.source_b.daily_query_quota = 2
    provider = FakeProvider(results=[])
    patched(FakeScraper())

    result = await SearchSource(config, store, provider).collect()

    assert result.ok and result.stopped_early
    assert provider.calls == ["q1", "q2"]
    assert result.notes["queries_run"] == 2


@pytest.mark.asyncio
async def test_provider_reporting_quota_exhaustion_burns_the_day(config, store, patched):
    config.source_b.queries = ["q1", "q2"]
    config.source_b.daily_query_quota = 10
    provider = FakeProvider(error=QuotaExhausted("429"))
    patched(FakeScraper())

    result = await SearchSource(config, store, provider).collect()

    assert result.ok and result.stopped_early
    assert provider.calls == ["q1"]
    assert store.quota_used("fake") >= config.source_b.daily_query_quota


@pytest.mark.asyncio
async def test_zero_results_is_a_normal_outcome(config, store, patched):
    provider = FakeProvider(results=[])
    patched(FakeScraper())

    result = await SearchSource(config, store, provider).collect()

    assert result.ok and result.errors == [] and result.posts == []


@pytest.mark.asyncio
async def test_all_queries_failing_is_a_broken_source(config, store, patched):
    config.source_b.queries = ["q1", "q2"]
    provider = FakeProvider(error=SearchError("500"))
    patched(FakeScraper())

    result = await SearchSource(config, store, provider).collect()

    assert not result.ok and "queries failed" in result.fatal


@pytest.mark.asyncio
async def test_scrapes_are_capped_per_cycle_and_the_rest_wait(config, store, patched):
    config.source_b.max_scrapes_per_cycle = 2
    urls = [f"https://www.threads.net/@dev/post/A{i}" for i in range(4)]
    provider = FakeProvider(results=urls)
    fake = patched(FakeScraper(posts={u: make_post(str(i), "dev") for i, u in enumerate(urls)}))

    await SearchSource(config, store, provider).collect()

    assert len(fake.post_calls) == 2
    assert not store.is_url_seen(urls[3])  # still available next cycle


@pytest.mark.asyncio
async def test_source_b_is_disabled_without_a_provider(config, store):
    assert SearchSource(config, store, None).enabled is False
    result = await SearchSource(config, store, None).collect()
    assert not result.ok


def test_scraper_user_agents_are_present():
    assert scraper_module.USER_AGENTS


@pytest.mark.asyncio
async def test_the_query_that_found_a_url_is_recorded(config, store, patched):
    config.source_b.queries = ['site:threads.net "loker"']
    url = "https://www.threads.net/@randomdev/post/AAA"
    provider = FakeProvider(results=[url])
    patched(FakeScraper(posts={url: make_post("1", "randomdev")}))

    await SearchSource(config, store, provider).collect()

    row = store.conn.execute("SELECT query, outcome FROM seen_urls WHERE url = ?", (url,)).fetchone()
    assert row["query"] == 'site:threads.net "loker"'
    assert row["outcome"] == "scraped"
