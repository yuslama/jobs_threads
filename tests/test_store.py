from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.parser import Post
from src.store import SourceState, Store


def make_post(pk="1", username="rina", caption="Loker"):
    return Post(pk=pk, code="A" + pk, username=username, caption=caption,
                taken_at=datetime.now(timezone.utc))


def test_first_sighting_inserts_and_second_does_not(store):
    post = make_post()
    assert store.mark_post_seen(post, "source_a", True) is True
    assert store.mark_post_seen(post, "source_b", True) is False
    assert store.is_post_seen("1")
    assert store.count_posts() == 1


def test_seen_posts_survive_a_restart(tmp_path):
    path = tmp_path / "persist.db"
    first = Store(path)
    first.mark_post_seen(make_post(), "source_a", True)
    first.record_url("https://www.threads.net/@rina/post/A1", "q", "scraped")
    first.close()

    second = Store(path)
    try:
        assert second.is_post_seen("1")
        assert second.is_url_seen("https://www.threads.net/@rina/post/A1")
    finally:
        second.close()


def test_new_urls_filters_out_known_ones(store):
    store.record_url("https://a/post/1", "q", "scraped")
    assert store.new_urls(["https://a/post/1", "https://a/post/2"]) == ["https://a/post/2"]


def test_dead_urls_stay_dead(store):
    store.record_url("https://a/post/1", "q", "dead")
    assert store.is_url_seen("https://a/post/1")


def test_lead_status_and_stats(store):
    for pk in ("1", "2"):
        store.save_lead(pk, "rina", "source_b", "ui designer", "a@b.com", "u", {})
    store.mark_lead_notified("1")
    assert store.set_lead_status("1", "applied") is True
    assert store.set_lead_status("missing", "applied") is False

    stats = store.lead_stats()
    assert stats["total"] == 2
    assert stats["source_b"] == 2
    assert stats["applied"] == 1
    assert store.get_lead("1")["status"] == "applied"


def test_saving_a_lead_again_keeps_its_status(store):
    store.save_lead("1", "rina", "source_b", "role", None, "u", {})
    store.set_lead_status("1", "applied")
    store.save_lead("1", "rina", "source_b", "role", None, "u", {"changed": True})
    assert store.get_lead("1")["status"] == "applied"


def test_notification_queue_round_trip(store):
    store.enqueue_notification("lead", {"text": "hi", "pk": "1"}, lead_pk="1")
    pending = store.pending_notifications()
    assert len(pending) == 1 and pending[0]["payload"]["text"] == "hi"
    assert store.bump_notification_attempt(pending[0]["id"], "boom") == 1
    store.drop_notification(pending[0]["id"])
    assert store.pending_notifications() == []


def test_account_hits_count_distinct_posts(store):
    assert store.record_account_hit("rina", "1", "u1") == 1
    assert store.record_account_hit("rina", "1", "u1") == 1  # same post again
    assert store.record_account_hit("rina", "2", "u2") == 2
    assert store.account_hit_urls("rina") == ["u2", "u1"]


def test_an_account_is_only_suggested_once(store):
    assert store.mark_account_suggested("rina", 2) is True
    assert store.mark_account_suggested("rina", 3) is False
    assert store.is_account_suggested("rina")


def test_quota_is_counted_per_day_and_provider(store):
    today = "2026-01-01"
    assert store.quota_used("google_cse", today) == 0
    store.consume_quota("google_cse", 1, today)
    store.consume_quota("google_cse", 2, today)
    assert store.quota_used("google_cse", today) == 3
    assert store.quota_used("google_cse", "2026-01-02") == 0
    assert store.quota_used("brave", today) == 0


def test_source_state_persists_and_reports_pausing(store):
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    store.save_source_state(SourceState("source_a", consecutive_failures=2, backoff_level=1,
                                        paused_until=until, alerted=True))
    state = store.get_source_state("source_a")
    assert state.consecutive_failures == 2
    assert state.backoff_level == 1
    assert state.alerted is True
    assert state.is_paused() is True
    assert state.is_paused(now=until + timedelta(minutes=1)) is False
    assert store.get_source_state("never_seen").consecutive_failures == 0


def test_first_poll_is_true_exactly_once(store):
    assert store.is_first_poll("rina") is True
    assert store.is_first_poll("rina") is False


def test_filtered_posts_are_logged_with_reasons(store):
    store.log_filtered(make_post(caption="MLM"), "source_b", "excluded by: mlm")
    rows = store.recent_filtered()
    assert rows[0]["reason"] == "excluded by: mlm"
