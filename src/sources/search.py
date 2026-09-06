"""Source B: keyword search (R2 + R3).

Slow and wide. Meta opened public Threads content to search engines in July
2025, so `site:threads.net "keyword"` reaches posts from accounts nobody knows
to watch. Lossy and delayed by indexing, which is fine: this is the bonus
channel, never the only one.

Stage 1 turns queries into post URLs. Stage 2 scrapes each genuinely new URL
through the same parser Source A uses.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..parser import is_post_url
from ..scraper import NotAvailable, RateLimited, ScrapeError, ThreadsScraper, polite_delay
from ..search_api import QuotaExhausted, SearchError, SearchProvider
from ..store import Store
from .base import Source, SourceResult

log = logging.getLogger(__name__)


class SearchSource(Source):
    name = "source_b"

    def __init__(self, config: Config, store: Store, provider: SearchProvider | None) -> None:
        self.config = config
        self.store = store
        self.provider = provider

    @property
    def enabled(self) -> bool:
        return (
            self.config.source_b.enabled
            and self.provider is not None
            and bool(self.config.source_b.queries)
        )

    async def collect(self) -> SourceResult:
        result = SourceResult(source=self.name)
        if self.provider is None:
            result.fatal = "no search provider configured"
            return result

        found, result.stopped_early = await self._run_queries(result)
        if result.fatal:
            return result

        new_urls = self.store.new_urls(found.keys())
        result.notes["urls_found"] = len(found)
        result.notes["urls_new"] = len(new_urls)
        log.info("source_b: %d urls, %d new", len(found), len(new_urls))

        await self._scrape_urls(new_urls, found, result)
        return result

    async def _run_queries(self, result: SourceResult) -> tuple[dict[str, str], bool]:
        """Stage 1: queries in, post URLs out.

        Returns ({url: query that found it}, stopped_early). The query is kept
        so a later tuning report can say which queries earn their quota.
        """
        settings = self.config.source_b
        assert self.provider is not None
        provider = self.provider
        seen: dict[str, str] = {}
        ran = 0
        stopped_early = False

        for query in settings.queries:
            used = self.store.quota_used(provider.name)
            if settings.daily_query_quota and used >= settings.daily_query_quota:
                # A normal outcome, not an error: log it, skip the rest, and
                # pick the list back up next cycle (R2).
                log.info(
                    "source_b: daily quota for %s exhausted (%d/%d), skipping %d queries",
                    provider.name, used, settings.daily_query_quota,
                    len(settings.queries) - ran,
                )
                stopped_early = True
                break

            try:
                raw = await provider.search(query, settings.results_per_query, settings.date_restrict)
                self.store.consume_quota(provider.name)
                ran += 1
            except QuotaExhausted as exc:
                log.info("source_b: provider reports quota exhausted: %s", exc)
                # Burn the rest of today's allowance so later cycles do not
                # keep hammering a provider that has already said no.
                self.store.consume_quota(provider.name, max(1, settings.daily_query_quota))
                stopped_early = True
                break
            except SearchError as exc:
                log.warning("source_b: query %r failed: %s", query, exc)
                result.errors.append(f"query {query!r}: {exc}")
                continue

            # Profile pages, tag pages and everything off-domain are dropped
            # here, so only real post URLs ever reach the scraper (R2).
            posts = [u for u in raw if is_post_url(u)]
            if not posts:
                # Zero results for a query is a normal outcome, not an error.
                log.info("source_b: query %r returned no post urls", query)
            for url in posts:
                seen.setdefault(url, query)

        if ran == 0 and not stopped_early and result.errors:
            result.fatal = f"all {len(result.errors)} queries failed"
        result.notes["queries_run"] = ran
        return seen, stopped_early

    async def _scrape_urls(self, urls: list[str], queries: dict[str, str],
                           result: SourceResult) -> None:
        """Stage 2: each new URL through the shared parser (R3)."""
        settings = self.config.source_b
        batch = urls[: settings.max_scrapes_per_cycle]
        for url in urls[settings.max_scrapes_per_cycle:]:
            # Left for the next cycle rather than dropped, so a noisy day does
            # not quietly lose leads.
            log.debug("source_b: deferring %s to next cycle", url)
        if not batch:
            return

        async with ThreadsScraper(
            headless=self.config.runtime.headless,
            timeout_seconds=self.config.runtime.page_timeout_seconds,
            chromium_path=self.config.runtime.chromium_path,
        ) as scraper:
            for index, url in enumerate(batch):
                if index:
                    await polite_delay(settings.scrape_delay_seconds)
                query = queries.get(url)
                try:
                    post = await scraper.fetch_post(url)
                except RateLimited as exc:
                    result.fatal = f"rate limited scraping {url}: {exc}"
                    return
                except NotAvailable:
                    # Gone for good: remember it so it is never retried (R3).
                    self.store.record_url(url, query, "dead")
                    result.errors.append(f"{url}: unavailable")
                    continue
                except ScrapeError as exc:
                    log.warning("source_b: %s failed: %s", url, exc)
                    result.errors.append(f"{url}: {exc}")
                    # No outcome recorded, so a transient failure is retried.
                    continue

                if post is None:
                    self.store.record_url(url, query, "dead")
                    continue
                self.store.record_url(url, query, "scraped", post_pk=post.pk)
                result.posts.append(post)
