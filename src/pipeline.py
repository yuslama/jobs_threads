"""The shared pipeline: dedupe, filter, extract, draft, notify, discover.

Both sources hand their posts to the same code path, so a post reached by
either one is treated identically and a post reached by both notifies once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .discovery import AccountDiscovery
from .drafts import DraftBuilder
from .extract import Extraction, extract_all
from .filters import FilterResult, KeywordFilter
from .notify import Notifier
from .parser import Post
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Lead:
    """A post that passed the filter, with everything the notification needs."""

    post: Post
    source: str
    extraction: Extraction
    filter_result: FilterResult
    draft: str | None = None


@dataclass
class PipelineStats:
    seen: int = 0
    duplicates: int = 0
    filtered: int = 0
    leads: int = 0
    notified: int = 0
    queued: int = 0
    suggestions: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.seen} posts, {self.duplicates} dupes, {self.filtered} filtered, "
            f"{self.leads} leads, {self.notified} notified, {self.queued} queued"
        )


class Pipeline:
    def __init__(self, config: Config, store: Store, notifier: Notifier) -> None:
        self.config = config
        self.store = store
        self.notifier = notifier
        self.filter = KeywordFilter(config.filters.include, config.filters.exclude)
        self.drafts = DraftBuilder(config)
        self.discovery = AccountDiscovery(config, store)

    async def process(self, posts: list[Post], source: str) -> PipelineStats:
        stats = PipelineStats(seen=len(posts))

        for post in posts:
            # mark_post_seen is the dedupe gate for both sources at once: the
            # insert only succeeds for the first sighting of a pk (R4).
            if self.store.is_post_seen(post.pk):
                stats.duplicates += 1
                continue

            result = self.filter.evaluate(post.caption)
            if not self.store.mark_post_seen(post, source, passed_filter=result.passed):
                # Another cycle inserted it between the check and the write.
                stats.duplicates += 1
                continue

            if not result.passed:
                stats.filtered += 1
                # Every drop is logged with its reason, because a filter that
                # is too tight loses leads silently (PRD section 10).
                self.store.log_filtered(post, source, result.reason)
                log.debug("filtered %s from @%s: %s", post.pk, post.username, result.reason)
                continue

            lead = self._build_lead(post, source, result)
            stats.leads += 1
            self._save_lead(lead)

            if await self.notifier.send_lead(lead):
                stats.notified += 1
            else:
                stats.queued += 1

            await self._maybe_suggest(lead, stats)

        return stats

    def _build_lead(self, post: Post, source: str, result: FilterResult) -> Lead:
        extraction = extract_all(post.caption, self.config.filters.role_titles)
        lead = Lead(post=post, source=source, extraction=extraction, filter_result=result)
        # A missing email is not a dropped lead; it is a lead without a draft.
        lead.draft = self.drafts.build(post, extraction)
        return lead

    def _save_lead(self, lead: Lead) -> None:
        self.store.save_lead(
            pk=lead.post.pk,
            username=lead.post.username,
            source=lead.source,
            role=lead.extraction.role,
            email=lead.extraction.email,
            url=lead.post.url,
            payload={
                "post": lead.post.to_row(),
                "emails": lead.extraction.emails,
                "urls": lead.extraction.urls,
                "deadline": lead.extraction.deadline,
                "matched": list(lead.filter_result.matched_include),
                "has_draft": bool(lead.draft),
            },
        )

    async def _maybe_suggest(self, lead: Lead, stats: PipelineStats) -> None:
        suggestion = self.discovery.consider(
            lead.post.username, lead.post.pk, lead.post.url, lead.source
        )
        if suggestion is None:
            return
        await self.notifier.send_suggestion(
            suggestion.username, suggestion.hits, suggestion.post_urls
        )
        stats.suggestions.append(suggestion.username)
        log.info("discovery: suggested @%s after %d hits", suggestion.username, suggestion.hits)
