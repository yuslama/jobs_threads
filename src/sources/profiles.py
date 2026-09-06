"""Source A: watched-account polling (R1).

Fast and narrow. The profile page already carries recent posts, so this is a
real new-post feed rather than a per-URL lookup, and latency is just the poll
interval. Blind to everything outside the watch list, which is what Source B
is for.
"""

from __future__ import annotations

import logging

from ..config import Config, normalise_username
from ..parser import Post
from ..scraper import NotAvailable, RateLimited, ScrapeError, ThreadsScraper, polite_delay
from ..store import Store
from .base import Source, SourceResult

log = logging.getLogger(__name__)


class ProfileSource(Source):
    name = "source_a"

    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store

    @property
    def enabled(self) -> bool:
        return self.config.source_a.enabled and bool(self.config.source_a.watched_accounts)

    async def collect(self) -> SourceResult:
        settings = self.config.source_a
        result = SourceResult(source=self.name)
        accounts = [normalise_username(a) for a in settings.watched_accounts]
        result.notes["accounts_polled"] = 0

        async with ThreadsScraper(
            headless=self.config.runtime.headless,
            timeout_seconds=self.config.runtime.page_timeout_seconds,
            chromium_path=self.config.runtime.chromium_path,
        ) as scraper:
            for index, account in enumerate(accounts):
                if index:
                    waited = await polite_delay(settings.delay_seconds)
                    log.debug("waited %.1fs before @%s", waited, account)
                try:
                    posts = await scraper.fetch_profile(account, limit=settings.posts_per_profile)
                except RateLimited as exc:
                    # The whole IP is throttled; carrying on just digs deeper.
                    result.fatal = f"rate limited on @{account}: {exc}"
                    log.warning("rate limited while polling @%s, aborting cycle", account)
                    return result
                except NotAvailable as exc:
                    # Private or deleted: warn and keep the loop alive (R1).
                    log.warning("profile @%s unavailable: %s", account, exc)
                    result.errors.append(f"@{account}: unavailable")
                    continue
                except ScrapeError as exc:
                    log.warning("profile @%s failed: %s", account, exc)
                    result.errors.append(f"@{account}: {exc}")
                    continue

                result.notes["accounts_polled"] += 1
                result.posts.extend(self._filter_by_age(account, posts))

        # Every account failing is a broken source, not ten bad accounts.
        if accounts and result.notes["accounts_polled"] == 0:
            result.fatal = f"all {len(accounts)} profiles failed"
        return result

    def _filter_by_age(self, account: str, posts: list[Post]) -> list[Post]:
        """On an account's first ever poll, skip its backlog.

        Without this, adding an account to the watch list dumps every recent
        post it has into the chat at once.
        """
        first_poll = self.store.is_first_poll(account)
        cutoff = self.config.source_a.first_run_max_age_hours
        if not first_poll or cutoff <= 0:
            return posts

        kept: list[Post] = []
        for post in posts:
            age = post.age_hours
            if age is not None and age > cutoff:
                # Mark seen so it never notifies later, but do not count it as
                # a lead: it is backlog, not news.
                self.store.mark_post_seen(post, self.name, passed_filter=False)
                continue
            kept.append(post)
        skipped = len(posts) - len(kept)
        if skipped:
            log.info("@%s first poll: skipped %d posts older than %dh", account, skipped, cutoff)
        return kept
