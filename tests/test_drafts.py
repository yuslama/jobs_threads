from __future__ import annotations

from datetime import datetime, timezone

from src.drafts import DraftBuilder, guess_company
from src.extract import Extraction, extract_all
from src.parser import Post


def make_post(caption: str) -> Post:
    return Post(pk="1", code="A1", username="lokerjakarta", caption=caption,
                taken_at=datetime.now(timezone.utc))


def test_draft_is_rendered_with_no_placeholders_left(config):
    caption = "Loker UI Designer di PT Kreatif Nusantara, cv ke hr@kreatif.co.id"
    post = make_post(caption)
    draft = DraftBuilder(config).build(post, extract_all(caption, config.filters.role_titles))
    assert draft is not None
    assert "{" not in draft and "}" not in draft
    assert "Rina" in draft
    assert "ui designer" in draft.lower()
    assert "PT Kreatif Nusantara" in draft
    assert post.url in draft


def test_no_email_means_no_draft(config):
    caption = "Loker UI Designer, DM aja"
    post = make_post(caption)
    assert DraftBuilder(config).build(post, extract_all(caption, config.filters.role_titles)) is None


def test_draft_disabled_returns_nothing(config):
    config.drafts.enabled = False
    caption = "Loker, cv ke hr@x.co.id"
    assert DraftBuilder(config).build(make_post(caption), extract_all(caption)) is None


def test_template_edits_are_picked_up_without_a_restart(config, tmp_path):
    builder = DraftBuilder(config)
    caption = "Loker, cv ke hr@x.co.id"
    extraction = extract_all(caption)
    first = builder.build(make_post(caption), extraction)
    assert first is not None and "Subject:" in first

    from pathlib import Path
    Path(config.drafts.template).write_text("Halo {company}, saya {seeker_name}.", encoding="utf-8")
    # mtime granularity can hide a same-second edit; force a distinct stamp.
    import os
    os.utime(config.drafts.template, (0, 0))
    second = builder.build(make_post(caption), extraction)
    assert second == "Halo X, saya Rina."


def test_company_guess_ignores_free_mailboxes():
    assert guess_company("Loker", Extraction(emails=["someone@gmail.com"]), "lokerjkt") == "@lokerjkt"
    assert guess_company("Loker", Extraction(emails=["hr@kreatif.co.id"]), "lokerjkt") == "Kreatif"
    assert guess_company("Loker di PT Maju Jaya", Extraction(), "lokerjkt") == "PT Maju Jaya"
