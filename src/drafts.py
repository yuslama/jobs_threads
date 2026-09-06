"""Draft email generation (R8).

The bot drafts. It does not send. There is deliberately no send code path
anywhere in this project -- see PRD section 9 for why, and read it before
adding one. The expensive part of applying was never clicking send, it was
staring at a blank compose window.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import Config
from .extract import Extraction
from .parser import Post

log = logging.getLogger(__name__)

COMPANY_RE = re.compile(r"\b((?:PT|CV|PT\.|CV\.)\s+[A-Z][A-Za-z0-9&.\- ]{2,40})")


def guess_company(caption: str, extraction: Extraction, username: str) -> str:
    """Best-guess company name, falling back to the posting account."""
    match = COMPANY_RE.search(caption or "")
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    email = extraction.email
    if email and "@" in email:
        domain = email.split("@", 1)[1].lower()
        # A free mailbox tells you nothing about the company; a company domain
        # usually is the company.
        free = {"gmail.com", "yahoo.com", "yahoo.co.id", "hotmail.com", "outlook.com",
                "icloud.com", "proton.me", "protonmail.com", "mail.com"}
        if domain not in free:
            return domain.split(".")[0].capitalize()
    return f"@{username}"


class DraftBuilder:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._template: str | None = None
        self._template_mtime: float | None = None

    def _load_template(self) -> str | None:
        """Read the template fresh when it changes, so her edits take effect
        without a restart."""
        path = Path(self.config.drafts.template)
        if not path.exists():
            log.warning("draft template missing: %s", path)
            return None
        mtime = path.stat().st_mtime
        if self._template is None or mtime != self._template_mtime:
            self._template = path.read_text(encoding="utf-8")
            self._template_mtime = mtime
        return self._template

    def build(self, post: Post, extraction: Extraction) -> str | None:
        """The draft for a lead, or None when there is no email to send it to."""
        if not self.config.drafts.enabled or not extraction.email:
            return None
        template = self._load_template()
        if template is None:
            return None

        seeker = self.config.seeker
        values = {
            "role": extraction.role or "posisi yang dibuka",
            "company": guess_company(post.caption, extraction, post.username),
            "account": post.username,
            "email": extraction.email,
            "post_url": post.url,
            "deadline": extraction.deadline or "-",
            "seeker_name": seeker.name,
            "seeker_email": seeker.email,
            "seeker_phone": seeker.phone,
            "seeker_portfolio": seeker.portfolio,
        }
        draft = template
        for key, value in values.items():
            draft = draft.replace("{" + key + "}", str(value))
        # Blank optional fields would otherwise leave ragged empty lines in
        # something she is about to paste into a mail client.
        draft = re.sub(r"\n{3,}", "\n\n", draft)
        return draft.strip()
