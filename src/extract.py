"""Pull the useful fields out of a free-text caption (R6).

Nothing here is allowed to drop a lead. A missing field is a missing field: no
email just means no draft and an apply method of "unknown".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+\s?(?:@|\(at\)|\[at\])\s?[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\"'\)]+", re.IGNORECASE)

HIRING_LEAD_INS = [
    r"(?:lagi\s+)?(?:cari|nyari|butuh|mencari|membutuhkan|dibutuhkan|dicari)",
    r"(?:we(?:'re| are)?\s+)?hiring(?:\s+(?:a|an|for))?",
    r"looking\s+for(?:\s+(?:a|an))?",
    r"open(?:ing)?\s+(?:position|role|recruitment)(?:\s+for)?",
    r"loker",
    r"lowongan(?:\s+kerja)?",
]
LEAD_IN_RE = re.compile(
    r"(?:%s)\s*[:\-]?\s*(?P<role>[A-Za-z][A-Za-z/&\.\+ ]{2,40})" % "|".join(HIRING_LEAD_INS),
    re.IGNORECASE,
)

MONTHS = {
    "januari": 1, "february": 2, "februari": 2, "january": 1, "maret": 3, "march": 3,
    "april": 4, "mei": 5, "may": 5, "juni": 6, "june": 6, "juli": 7, "july": 7,
    "agustus": 8, "august": 8, "september": 9, "oktober": 10, "october": 10,
    "november": 11, "desember": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b")
WORD_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_ALT})\b(?:\s+(\d{{4}}))?", re.IGNORECASE)
DEADLINE_CUE_RE = re.compile(
    r"(deadline|dateline|batas\s+(?:waktu|akhir)|closing|close[sd]?\s+on|paling\s+lambat|"
    r"ditutup|sampai\s+(?:dengan|tgl|tanggal)?|until|hingga|s/d)",
    re.IGNORECASE,
)

# Trailing words that are part of the sentence, not part of the job title.
ROLE_TAIL_RE = re.compile(
    r"\b(?:untuk|buat|di|at|for|with|yang|dengan|yg|dm|kirim|send|email|wa|silakan|please)\b.*$",
    re.IGNORECASE,
)


@dataclass
class Extraction:
    emails: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    role: str | None = None
    deadline: str | None = None

    @property
    def email(self) -> str | None:
        return self.emails[0] if self.emails else None

    @property
    def apply_method(self) -> str:
        if self.emails:
            return f"email: {self.emails[0]}"
        if self.urls:
            return f"link: {self.urls[0]}"
        return "unknown (check the post)"


def extract_emails(text: str) -> list[str]:
    found: list[str] = []
    for raw in EMAIL_RE.findall(text or ""):
        email = re.sub(r"\s*(?:\(at\)|\[at\])\s*", "@", raw, flags=re.IGNORECASE)
        email = email.replace(" ", "").strip(".,;:")
        if email.lower() not in [f.lower() for f in found]:
            found.append(email)
    return found


def extract_urls(text: str) -> list[str]:
    found: list[str] = []
    for url in URL_RE.findall(text or ""):
        url = url.rstrip(".,;:)")
        if url not in found:
            found.append(url)
    return found


def _clean_role(candidate: str) -> str:
    role = ROLE_TAIL_RE.sub("", candidate)
    role = re.sub(r"\s+", " ", role).strip(" -:.,&/")
    return role


def guess_role(text: str, role_titles: list[str] | None = None) -> str | None:
    """Best guess only. A wrong guess costs a squint, not a lead."""
    lowered = (text or "").lower()

    # A configured title is the strongest signal: prefer the earliest one.
    best: tuple[int, str] | None = None
    for title in role_titles or []:
        idx = lowered.find(title.lower())
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, title)
    if best:
        return best[1]

    match = LEAD_IN_RE.search(text or "")
    if match:
        role = _clean_role(match.group("role"))
        if 2 < len(role) <= 45:
            return role
    return None


def _normalise_numeric(day: str, month: str, year: str | None) -> str | None:
    d, m = int(day), int(month)
    if not (1 <= d <= 31 and 1 <= m <= 12):
        return None
    if year:
        y = int(year)
        if y < 100:
            y += 2000
        return f"{d:02d}/{m:02d}/{y}"
    return f"{d:02d}/{m:02d}"


def extract_deadline(text: str) -> str | None:
    """A date near a deadline cue, else the first date-looking thing at all."""
    text = text or ""
    for cue in DEADLINE_CUE_RE.finditer(text):
        window = text[cue.end(): cue.end() + 60]
        word = WORD_DATE_RE.search(window)
        if word:
            day, month, year = word.group(1), word.group(2), word.group(3)
            return f"{int(day):02d} {month.capitalize()}" + (f" {year}" if year else "")
        numeric = NUMERIC_DATE_RE.search(window)
        if numeric:
            normalised = _normalise_numeric(*numeric.groups())
            if normalised:
                return normalised
    word = WORD_DATE_RE.search(text)
    if word:
        day, month, year = word.group(1), word.group(2), word.group(3)
        return f"{int(day):02d} {month.capitalize()}" + (f" {year}" if year else "")
    numeric = NUMERIC_DATE_RE.search(text)
    if numeric:
        return _normalise_numeric(*numeric.groups())
    return None


def extract_all(caption: str, role_titles: list[str] | None = None) -> Extraction:
    return Extraction(
        emails=extract_emails(caption),
        urls=extract_urls(caption),
        role=guess_role(caption, role_titles),
        deadline=extract_deadline(caption),
    )
