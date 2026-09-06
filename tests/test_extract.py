from __future__ import annotations

from src.extract import extract_all, extract_deadline, extract_emails, extract_urls, guess_role

ROLES = ["ui designer", "content writer", "social media specialist"]


def test_extracts_email_and_obfuscated_email():
    assert extract_emails("kirim ke hr@studio.co.id ya") == ["hr@studio.co.id"]
    assert extract_emails("email: rina (at) company.com") == ["rina@company.com"]


def test_extracts_multiple_emails_without_duplicates():
    caption = "cv ke a@x.com atau a@x.com atau b@y.co.id"
    assert extract_emails(caption) == ["a@x.com", "b@y.co.id"]


def test_extracts_urls_and_strips_trailing_punctuation():
    assert extract_urls("apply di https://bit.ly/abc.") == ["https://bit.ly/abc"]


def test_role_prefers_a_configured_title():
    assert guess_role("Loker: butuh UI Designer buat tim kami", ROLES) == "ui designer"


def test_role_falls_back_to_hiring_lead_in_phrasing():
    assert guess_role("Tim gue lagi cari Motion Grapher untuk project baru", ROLES) == "Motion Grapher"
    assert guess_role("We're hiring a Backend Engineer with 3 years", ROLES) == "Backend Engineer"


def test_role_is_none_when_nothing_looks_like_a_title():
    assert guess_role("Selamat pagi semuanya", ROLES) is None


def test_deadline_near_a_cue_wins():
    assert extract_deadline("Loker dibuka, deadline 30 September 2025") == "30 September 2025"
    assert extract_deadline("paling lambat 15/10/2025") == "15/10/2025"
    assert extract_deadline("batas akhir 5-9") == "05/09"


def test_deadline_is_none_when_no_date_appears():
    assert extract_deadline("kirim cv sekarang") is None


def test_apply_method_degrades_from_email_to_link_to_unknown():
    with_email = extract_all("Loker, cv ke hr@x.com", ROLES)
    assert with_email.apply_method.startswith("email: hr@x.com")

    with_link = extract_all("Loker, apply https://x.com/form", ROLES)
    assert with_link.apply_method.startswith("link:")

    neither = extract_all("Loker, DM aja", ROLES)
    assert neither.apply_method.startswith("unknown")
    assert neither.email is None
