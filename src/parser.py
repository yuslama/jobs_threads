"""Turn Threads' hidden JSON blobs into clean Post objects.

Meta embeds the rendered data as JSON inside <script type="application/json"
data-sjs> elements. The shape shifts between deploys, so nothing here walks a
fixed index path: nested_lookup finds the post payloads wherever they sit and
jmespath maps the fields out of each one. This is the single parser behind both
sources (R1 and R3 share it).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import jmespath
from nested_lookup import nested_lookup

log = logging.getLogger(__name__)

SCRIPT_RE = re.compile(
    r'<script[^>]+type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Keys under which Threads has been seen to hang post payloads. Trying several
# is cheaper than pinning one and going silently blind when it is renamed.
POST_CONTAINER_KEYS = ("thread_items", "post", "items")

POST_MAPPING = jmespath.compile(
    """{
        pk: pk,
        code: code,
        username: user.username,
        user_pk: user.pk,
        caption: caption.text,
        taken_at: taken_at,
        like_count: like_count,
        reply_count: text_post_app_info.direct_reply_count,
        reposted: text_post_app_info.is_post_unavailable,
        parent_pk: text_post_app_info.reply_to_author.pk
    }"""
)


@dataclass
class Post:
    """One Threads post, source-agnostic."""

    pk: str
    code: str
    username: str
    caption: str
    taken_at: datetime | None = None
    url: str = ""
    like_count: int = 0
    reply_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.url and self.username and self.code:
            self.url = post_url(self.username, self.code)

    @property
    def age_hours(self) -> float | None:
        if self.taken_at is None:
            return None
        return (datetime.now(timezone.utc) - self.taken_at).total_seconds() / 3600

    def to_row(self) -> dict[str, Any]:
        return {
            "pk": self.pk,
            "code": self.code,
            "username": self.username,
            "caption": self.caption,
            "taken_at": self.taken_at.isoformat() if self.taken_at else None,
            "url": self.url,
        }


def post_url(username: str, code: str) -> str:
    return f"https://www.threads.net/@{username.lstrip('@')}/post/{code}"


def parse_post_url(url: str) -> tuple[str, str] | None:
    """Pull (username, code) out of a Threads post URL, or None if it is not one."""
    match = re.match(
        r"^https?://(?:www\.)?threads\.(?:net|com)/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)",
        url.strip(),
    )
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


def is_post_url(url: str) -> bool:
    """True only for individual post URLs. Profile and tag pages are not posts."""
    return parse_post_url(url) is not None


def _to_datetime(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def extract_json_blobs(html: str) -> list[Any]:
    """Every parseable hidden JSON payload in the page."""
    blobs: list[Any] = []
    for chunk in SCRIPT_RE.findall(html or ""):
        chunk = chunk.strip()
        if not chunk or chunk[0] not in "{[":
            continue
        try:
            blobs.append(json.loads(chunk))
        except json.JSONDecodeError:
            continue
    return blobs


def _candidate_dicts(blobs: Iterable[Any]) -> list[dict[str, Any]]:
    """Every dict that could be a post payload, from anywhere in the blobs."""
    found: list[dict[str, Any]] = []
    for blob in blobs:
        for key in POST_CONTAINER_KEYS:
            for hit in nested_lookup(key, blob):
                if isinstance(hit, dict):
                    found.append(hit)
                elif isinstance(hit, list):
                    found.extend(item for item in hit if isinstance(item, dict))
    return found


def _looks_like_post(candidate: dict[str, Any]) -> bool:
    return bool(candidate.get("pk") and candidate.get("code") and isinstance(candidate.get("user"), dict))


def _flatten(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """A thread_items wrapper holds the real post under 'post'; unwrap those."""
    if _looks_like_post(candidate):
        return [candidate]
    inner = candidate.get("post")
    if isinstance(inner, dict) and _looks_like_post(inner):
        return [inner]
    return []


def parse_posts(html: str, username_hint: str | None = None) -> list[Post]:
    """All posts found in a profile or post page, de-duplicated, newest first."""
    posts: dict[str, Post] = {}
    for candidate in _candidate_dicts(extract_json_blobs(html)):
        for payload in _flatten(candidate):
            post = _build_post(payload, username_hint)
            if post is None:
                continue
            # The same post appears in several blobs; keep the richest caption.
            existing = posts.get(post.pk)
            if existing is None or len(post.caption) > len(existing.caption):
                posts[post.pk] = post
    ordered = sorted(
        posts.values(),
        key=lambda p: p.taken_at or datetime.fromtimestamp(0, tz=timezone.utc),
        reverse=True,
    )
    return ordered


def _build_post(payload: dict[str, Any], username_hint: str | None) -> Post | None:
    mapped = POST_MAPPING.search(payload) or {}
    pk = mapped.get("pk")
    code = mapped.get("code")
    if not pk or not code:
        return None
    username = (mapped.get("username") or username_hint or "").lstrip("@").lower()
    if not username:
        return None
    caption = mapped.get("caption") or ""
    if not isinstance(caption, str):
        caption = str(caption)
    return Post(
        pk=str(pk),
        code=str(code),
        username=username,
        caption=caption.strip(),
        taken_at=_to_datetime(mapped.get("taken_at")),
        like_count=int(mapped.get("like_count") or 0),
        reply_count=int(mapped.get("reply_count") or 0),
        raw=payload,
    )


def parse_single_post(html: str, url: str) -> Post | None:
    """The post a post-page URL points at, ignoring replies rendered alongside it."""
    parsed = parse_post_url(url)
    if parsed is None:
        return None
    username, code = parsed
    posts = parse_posts(html, username_hint=username)
    for post in posts:
        if post.code == code:
            return post
    log.debug("post %s not found among %d parsed posts", code, len(posts))
    return None
