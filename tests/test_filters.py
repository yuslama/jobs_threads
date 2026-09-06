from __future__ import annotations

from src.filters import KeywordFilter

FILTER = KeywordFilter(
    include=["loker", "hiring", "lowongan", "we're hiring"],
    exclude=["mlm", "internship", "senior manager"],
)


def test_include_match_passes():
    result = FILTER.evaluate("Loker UI designer Jakarta")
    assert result.passed
    assert result.matched_include == ("loker",)


def test_exclude_wins_over_include():
    result = FILTER.evaluate("Loker MLM gaji besar")
    assert not result.passed
    assert "mlm" in result.reason
    assert result.matched_exclude == ("mlm",)


def test_no_include_keyword_is_dropped_with_a_reason():
    result = FILTER.evaluate("Hari ini cerah sekali")
    assert not result.passed
    assert result.reason == "no include keyword matched"


def test_matching_is_case_insensitive_and_survives_line_breaks():
    assert FILTER.evaluate("LOWONGAN\nkerja").passed
    assert FILTER.evaluate("we're\nhiring now").passed


def test_empty_caption_is_dropped():
    assert not FILTER.evaluate("").passed
    assert not FILTER.evaluate("   ").passed
