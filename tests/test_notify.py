from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.extract import extract_all
from src.filters import FilterResult
from src.notify import (
    CB_APPLIED, CB_DISMISS, build_draft_message, build_lead_message, lead_keyboard,
    make_callback_handler, preview,
)
from src.parser import Post
from src.pipeline import Lead


def make_lead(caption: str, source: str = "source_a", draft: str | None = None) -> Lead:
    post = Post(pk="1", code="A1", username="lokerjakarta", caption=caption,
                taken_at=datetime(2026, 9, 1, 10, 30, tzinfo=timezone.utc))
    return Lead(post=post, source=source, extraction=extract_all(caption, ["ui designer"]),
                filter_result=FilterResult(True, "matched: loker"), draft=draft)


def test_lead_message_carries_everything_needed_to_triage():
    lead = make_lead("Loker UI Designer Jakarta, kirim cv ke hr@kreatif.co.id, deadline 30 September")
    text = build_lead_message(lead)

    assert "ui designer" in text.lower()
    assert "@lokerjakarta" in text
    assert "watch list" in text
    assert "hr@kreatif.co.id" in text
    assert "30 September" in text
    assert "https://www.threads.net/@lokerjakarta/post/A1" in text


def test_message_names_the_source_that_found_it():
    assert "search" in build_lead_message(make_lead("Loker admin", source="source_b"))


def test_unknown_apply_method_is_stated_not_hidden():
    assert "unknown" in build_lead_message(make_lead("Loker admin, DM aja"))


def test_caption_is_previewed_at_300_chars():
    long_caption = "Loker " + "x" * 500
    text = build_lead_message(make_lead(long_caption))
    assert "..." in text
    assert len(preview(long_caption)) <= 303


def test_html_in_a_caption_cannot_break_the_message():
    text = build_lead_message(make_lead("Loker <b>designer</b> & co <script>alert(1)</script>"))
    assert "&lt;script&gt;" in text
    assert "<script>" not in text


def test_draft_message_only_exists_when_there_is_a_draft():
    assert build_draft_message(make_lead("Loker")) is None
    message = build_draft_message(make_lead("Loker", draft="Subject: Lamaran"))
    assert message is not None and "<pre>" in message


def test_keyboard_has_both_actions_bound_to_the_post():
    buttons = lead_keyboard("42").inline_keyboard[0]
    assert [b.text for b in buttons] == ["Mark applied", "Not relevant"]
    assert [b.callback_data for b in buttons] == [f"{CB_APPLIED}:42", f"{CB_DISMISS}:42"]


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers: list[str] = []
        self.markup_edits = 0

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_reply_markup(self, markup=None, **kwargs):
        self.markup_edits += 1


class FakeUpdate:
    def __init__(self, query):
        self.callback_query = query


@pytest.mark.asyncio
async def test_mark_applied_button_updates_the_lead(store):
    store.save_lead("42", "rina", "source_a", "ui designer", None, "u", {})
    handler = make_callback_handler(store)
    query = FakeQuery(f"{CB_APPLIED}:42")

    await handler.callback(FakeUpdate(query), None)

    assert store.get_lead("42")["status"] == "applied"
    assert query.answers == ["Marked applied"]
    assert query.markup_edits == 1


@pytest.mark.asyncio
async def test_not_relevant_button_dismisses_the_lead(store):
    store.save_lead("42", "rina", "source_a", None, None, "u", {})
    handler = make_callback_handler(store)

    await handler.callback(FakeUpdate(FakeQuery(f"{CB_DISMISS}:42")), None)

    assert store.get_lead("42")["status"] == "not_relevant"


@pytest.mark.asyncio
async def test_a_button_for_an_unknown_lead_is_handled_gracefully(store):
    handler = make_callback_handler(store)
    query = FakeQuery(f"{CB_APPLIED}:nope")

    await handler.callback(FakeUpdate(query), None)

    assert query.answers == ["Lead not found"]
    assert query.markup_edits == 0
