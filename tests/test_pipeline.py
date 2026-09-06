from __future__ import annotations

from datetime import datetime, timezone

import pytest
from telegram import InlineKeyboardMarkup

from src.notify import Notifier
from src.parser import Post
from src.pipeline import Pipeline
from src.store import Store


class RecordingNotifier(Notifier):
    """A notifier that records instead of sending."""

    def __init__(self, config, store, fail: bool = False):
        super().__init__(config, store, bot=None)
        self.sent: list[str] = []
        self.fail = fail

    async def _send(self, chat_id, text, keyboard: InlineKeyboardMarkup | None = None):
        if self.fail:
            from telegram.error import TelegramError
            raise TelegramError("telegram down")
        self.sent.append(text)


def make_post(pk: str, caption: str, username: str = "lokerjakarta") -> Post:
    return Post(pk=pk, code="C" + pk, username=username, caption=caption,
                taken_at=datetime.now(timezone.utc))


@pytest.fixture
def pipeline_parts(config, tmp_path):
    store = Store(tmp_path / "pipeline.db")
    notifier = RecordingNotifier(config, store)
    yield Pipeline(config, store, notifier), store, notifier
    store.close()


@pytest.mark.asyncio
async def test_passing_post_is_notified_once_with_a_draft(pipeline_parts):
    pipeline, store, notifier = pipeline_parts
    post = make_post("1", "Loker UI Designer Jakarta, kirim cv ke hr@kreatif.co.id")

    stats = await pipeline.process([post], "source_a")

    assert stats.leads == 1 and stats.notified == 1 and stats.queued == 0
    # Lead message plus the draft message.
    assert len(notifier.sent) == 2
    assert "ui designer" in notifier.sent[0].lower()
    assert "hr@kreatif.co.id" in notifier.sent[0]
    assert "Draft" in notifier.sent[1]
    assert store.get_lead("1")["status"] == "new"
    assert store.get_lead("1")["notified_at"] is not None


@pytest.mark.asyncio
async def test_the_same_post_from_the_other_source_does_not_notify_again(pipeline_parts):
    pipeline, store, notifier = pipeline_parts
    post = make_post("1", "Loker UI Designer, cv ke hr@x.co.id")

    await pipeline.process([post], "source_a")
    stats = await pipeline.process([post], "source_b")

    assert stats.duplicates == 1 and stats.leads == 0
    assert len(notifier.sent) == 2  # unchanged from the first pass


@pytest.mark.asyncio
async def test_excluded_post_is_dropped_and_logged(pipeline_parts):
    pipeline, store, notifier = pipeline_parts
    stats = await pipeline.process([make_post("1", "Loker MLM modal kecil")], "source_a")

    assert stats.filtered == 1 and stats.leads == 0
    assert notifier.sent == []
    logged = store.recent_filtered()
    assert logged[0]["reason"] == "excluded by: mlm"


@pytest.mark.asyncio
async def test_lead_without_an_email_is_still_sent(pipeline_parts):
    pipeline, store, notifier = pipeline_parts
    stats = await pipeline.process([make_post("1", "Loker content writer, DM aja")], "source_a")

    assert stats.notified == 1
    assert len(notifier.sent) == 1  # no draft message
    assert "unknown" in notifier.sent[0]


@pytest.mark.asyncio
async def test_undeliverable_lead_is_queued_not_lost(config, tmp_path):
    store = Store(tmp_path / "queue.db")
    try:
        notifier = RecordingNotifier(config, store, fail=True)
        pipeline = Pipeline(config, store, notifier)

        stats = await pipeline.process([make_post("1", "Loker UI designer, cv ke a@b.co.id")], "source_a")

        assert stats.queued == 1 and stats.notified == 0
        pending = store.pending_notifications()
        assert len(pending) == 1
        assert pending[0]["lead_pk"] == "1"
        assert store.get_lead("1")["notified_at"] is None

        # Telegram comes back: the queued lead goes out on the next flush.
        notifier.fail = False
        assert await notifier.flush_queue() == 1
        assert store.pending_notifications() == []
        assert store.get_lead("1")["notified_at"] is not None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_search_discovered_account_is_suggested_after_two_hits(pipeline_parts):
    pipeline, store, notifier = pipeline_parts

    first = await pipeline.process([make_post("1", "Loker UI designer", username="randomdev")], "source_b")
    assert first.suggestions == []

    second = await pipeline.process([make_post("2", "Loker content writer", username="randomdev")], "source_b")
    assert second.suggestions == ["randomdev"]
    assert any("Watch-list suggestion" in msg for msg in notifier.sent)

    third = await pipeline.process([make_post("3", "Loker admin", username="randomdev")], "source_b")
    assert third.suggestions == []  # suggested once, never again


@pytest.mark.asyncio
async def test_watched_accounts_are_never_suggested(pipeline_parts):
    pipeline, store, notifier = pipeline_parts
    watched = config_username = "lokerjakarta"  # already in the watch list

    for pk in ("1", "2", "3"):
        stats = await pipeline.process([make_post(pk, "Loker hiring", username=watched)], "source_b")
        assert stats.suggestions == []
    assert not store.is_account_suggested(config_username)


@pytest.mark.asyncio
async def test_source_a_hits_never_trigger_suggestions(pipeline_parts):
    pipeline, store, _ = pipeline_parts
    for pk in ("1", "2"):
        stats = await pipeline.process([make_post(pk, "Loker hiring", username="unwatched")], "source_a")
        assert stats.suggestions == []
