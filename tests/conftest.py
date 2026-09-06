from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import (  # noqa: E402
    Config, DiscoveryConfig, DraftConfig, FilterConfig, ReliabilityConfig,
    RuntimeConfig, Secrets, SeekerConfig, SourceAConfig, SourceBConfig,
)
from src.store import Store  # noqa: E402


def make_config(tmp_path: Path, **overrides) -> Config:
    config = Config(
        seeker=SeekerConfig(name="Rina", email="rina@example.com", phone="0812", portfolio="rina.design"),
        source_a=SourceAConfig(
            enabled=True,
            poll_interval_minutes=30,
            delay_seconds=(0, 0),
            first_run_max_age_hours=48,
            posts_per_profile=5,
            watched_accounts=["lokerjakarta"],
        ),
        source_b=SourceBConfig(
            enabled=True,
            search_interval_hours=6,
            scrape_delay_seconds=(0, 0),
            max_scrapes_per_cycle=3,
            results_per_query=10,
            date_restrict="d2",
            daily_query_quota=5,
            queries=['site:threads.net "loker"'],
        ),
        filters=FilterConfig(
            include=["loker", "hiring", "lowongan"],
            exclude=["mlm", "internship"],
            role_titles=["ui designer", "content writer"],
        ),
        discovery=DiscoveryConfig(enabled=True, suggest_after_hits=2),
        drafts=DraftConfig(enabled=True, template=str(tmp_path / "template.txt")),
        reliability=ReliabilityConfig(
            backoff_minutes=[2, 4, 8, 16, 32, 60],
            max_consecutive_failures=3,
            pause_minutes=60,
            zero_result_alert_streak=2,
            notify_max_attempts=3,
        ),
        runtime=RuntimeConfig(database=str(tmp_path / "test.db")),
        secrets=Secrets(telegram_bot_token="", telegram_chat_id="123"),
    )
    Path(config.drafts.template).write_text(
        "Subject: Lamaran {role} - {seeker_name}\n\n"
        "Halo {company},\n\nSaya {seeker_name}.\n\n{seeker_name}\n{seeker_email}\n"
        "--\n{post_url}\n",
        encoding="utf-8",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "store.db")
    yield s
    s.close()


def post_payload(pk: str, code: str, username: str, caption: str,
                 taken_at: datetime | None = None) -> dict:
    """A post shaped like the ones inside Threads' hidden JSON."""
    taken_at = taken_at or datetime.now(timezone.utc)
    return {
        "pk": pk,
        "code": code,
        "taken_at": int(taken_at.timestamp()),
        "like_count": 12,
        "user": {"pk": "999", "username": username},
        "caption": {"text": caption},
        "text_post_app_info": {"direct_reply_count": 3},
    }


def profile_html(posts: list[dict]) -> str:
    """A page carrying those posts the way Threads embeds them."""
    blob = {
        "require": [[
            "ScheduledServerJS", "handle", None,
            [{"__bbox": {"require": [[
                "RelayPrefetchedStreamCache", "next", [],
                [{"__bbox": {"result": {"data": {"mediaData": {"edges": [
                    {"node": {"thread_items": [{"post": p}]}} for p in posts
                ]}}}}}],
            ]]}}],
        ]]
    }
    return (
        "<html><head><title>Threads</title></head><body>"
        '<script type="application/json" data-content-len="1" data-sjs>'
        + json.dumps(blob)
        + "</script>"
        '<script type="application/json">{"unrelated": true}</script>'
        "<script>window.x = 1;</script>"
        "</body></html>"
    )
