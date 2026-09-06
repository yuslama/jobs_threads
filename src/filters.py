"""Keyword include/exclude filtering (R5).

Case-insensitive, Indonesian and English, applied identically to both sources.
Exclude always wins over include: a post that mentions a non-starter city is
dropped even if it also says "loker".
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str
    matched_include: tuple[str, ...] = ()
    matched_exclude: tuple[str, ...] = ()


def _normalise(text: str) -> str:
    # Collapse whitespace so a keyword spanning a line break still matches.
    return re.sub(r"\s+", " ", (text or "").lower())


def _matches(haystack: str, needles: list[str]) -> tuple[str, ...]:
    return tuple(n for n in needles if n.strip() and n.strip().lower() in haystack)


class KeywordFilter:
    def __init__(self, include: list[str], exclude: list[str]) -> None:
        self.include = [i.lower() for i in include]
        self.exclude = [e.lower() for e in exclude]

    def evaluate(self, caption: str) -> FilterResult:
        haystack = _normalise(caption)
        if not haystack:
            return FilterResult(False, "empty caption")

        excluded = _matches(haystack, self.exclude)
        if excluded:
            return FilterResult(
                False,
                f"excluded by: {', '.join(excluded)}",
                matched_exclude=excluded,
            )

        included = _matches(haystack, self.include)
        if not included:
            return FilterResult(False, "no include keyword matched")

        return FilterResult(True, f"matched: {', '.join(included)}", matched_include=included)
