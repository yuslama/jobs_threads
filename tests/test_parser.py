from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import post_payload, profile_html

from src.parser import is_post_url, parse_post_url, parse_posts, parse_single_post, post_url


def test_parses_posts_from_profile_html():
    now = datetime.now(timezone.utc)
    html = profile_html([
        post_payload("1001", "AbC123", "lokerjakarta", "Loker UI designer, kirim CV ke hr@studio.co.id", now),
        post_payload("1002", "DeF456", "lokerjakarta", "Halo semua", now - timedelta(hours=2)),
    ])
    posts = parse_posts(html)
    assert [p.pk for p in posts] == ["1001", "1002"]  # newest first
    assert posts[0].username == "lokerjakarta"
    assert posts[0].caption.startswith("Loker UI designer")
    assert posts[0].url == "https://www.threads.net/@lokerjakarta/post/AbC123"
    assert posts[0].taken_at is not None
    assert posts[0].reply_count == 3


def test_ignores_unparseable_and_irrelevant_scripts():
    assert parse_posts("<html><script>not json</script></html>") == []
    assert parse_posts("") == []


def test_deduplicates_a_post_appearing_in_several_blobs():
    payload = post_payload("1001", "AbC123", "lokerjakarta", "Loker penuh")
    html = profile_html([payload, payload])
    assert len(parse_posts(html)) == 1


def test_prefers_the_richest_caption_for_the_same_post():
    short = post_payload("1001", "AbC123", "lokerjakarta", "Loker")
    full = post_payload("1001", "AbC123", "lokerjakarta", "Loker UI designer di Jakarta")
    posts = parse_posts(profile_html([short, full]))
    assert posts[0].caption == "Loker UI designer di Jakarta"


def test_parse_single_post_picks_the_requested_code():
    html = profile_html([
        post_payload("1001", "AbC123", "rina", "Loker asli"),
        post_payload("1002", "ZzZ999", "someone", "balasan"),
    ])
    url = "https://www.threads.net/@rina/post/AbC123"
    post = parse_single_post(html, url)
    assert post is not None and post.pk == "1001"
    assert parse_single_post(html, "https://www.threads.net/@rina/post/NOPE") is None


def test_post_url_recognition_rejects_profile_and_tag_pages():
    assert is_post_url("https://www.threads.net/@rina/post/AbC123")
    assert is_post_url("https://threads.com/@rina/post/AbC-1_2")
    assert not is_post_url("https://www.threads.net/@rina")
    assert not is_post_url("https://www.threads.net/tag/loker")
    assert not is_post_url("https://example.com/@rina/post/AbC123")
    assert not is_post_url("")
    assert parse_post_url("https://www.threads.net/@Rina/post/AbC123") == ("rina", "AbC123")


def test_post_url_builder_strips_the_at_sign():
    assert post_url("@rina", "X1") == "https://www.threads.net/@rina/post/X1"
