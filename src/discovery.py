"""Account discovery (R9).

The feedback loop that makes the two-source design worth the extra code. When
Source B surfaces a vacancy from an account nobody is watching, count it. Once
an account has produced enough passing posts, suggest it to Yus for the fast
lane.

Promotion stays a manual config edit on purpose: auto-adding accounts means one
spam account poisons the fast lane permanently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config, normalise_username
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Suggestion:
    username: str
    hits: int
    post_urls: list[str]


class AccountDiscovery:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store

    def consider(self, username: str, post_pk: str, post_url: str, source: str) -> Suggestion | None:
        """Record a passing post and return a suggestion if this crosses the bar."""
        if not self.config.discovery.enabled:
            return None
        # Source A only ever sees watched accounts, so a hit from it tells us
        # nothing new.
        if source != "source_b":
            return None

        handle = normalise_username(username)
        if not handle:
            return None
        if handle in self.config.watched():
            return None
        if self.store.is_account_suggested(handle):
            return None

        hits = self.store.record_account_hit(handle, post_pk, post_url)
        if hits < self.config.discovery.suggest_after_hits:
            log.debug("discovery: @%s at %d hit(s)", handle, hits)
            return None

        # mark_account_suggested is the idempotency gate: exactly one
        # suggestion per account, even if two cycles cross the bar together.
        if not self.store.mark_account_suggested(handle, hits):
            return None
        return Suggestion(username=handle, hits=hits, post_urls=self.store.account_hit_urls(handle))
