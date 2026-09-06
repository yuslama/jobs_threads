from __future__ import annotations

import httpx
import pytest

from src.config import Secrets
from src.search_api import (
    BraveProvider, GoogleCSEProvider, QuotaExhausted, SearchError, get_provider,
)


def client_returning(status: int, payload=None, text: str = "") -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if payload is not None:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=text)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_google_returns_result_links():
    client = client_returning(200, {"items": [{"link": "https://www.threads.net/@a/post/1"}, {"link": "https://x"}]})
    provider = GoogleCSEProvider("key", "cx", client=client)
    assert await provider.search("q", 10, "d2") == ["https://www.threads.net/@a/post/1", "https://x"]


@pytest.mark.asyncio
async def test_google_403_for_quota_is_quota_and_403_for_a_bad_key_is_not():
    quota = GoogleCSEProvider("key", "cx", client=client_returning(403, text="dailyLimitExceeded"))
    with pytest.raises(QuotaExhausted):
        await quota.search("q", 10, "d2")

    bad_key = GoogleCSEProvider("key", "cx", client=client_returning(403, text="API key not valid"))
    with pytest.raises(SearchError) as exc:
        await bad_key.search("q", 10, "d2")
    assert not isinstance(exc.value, QuotaExhausted)


@pytest.mark.asyncio
async def test_google_429_is_quota_exhaustion():
    provider = GoogleCSEProvider("key", "cx", client=client_returning(429, text="too many"))
    with pytest.raises(QuotaExhausted):
        await provider.search("q", 10, "d2")


@pytest.mark.asyncio
async def test_brave_maps_date_restriction_to_freshness():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["freshness"] = request.url.params.get("freshness")
        return httpx.Response(200, json={"web": {"results": [{"url": "https://www.threads.net/@a/post/1"}]}})

    provider = BraveProvider("key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await provider.search("q", 10, "d2") == ["https://www.threads.net/@a/post/1"]
    assert captured["freshness"] == "pd"


def test_provider_selection_is_one_place():
    assert get_provider(Secrets(search_provider="none")) is None
    assert get_provider(Secrets(search_provider="google_cse", google_cse_api_key="k",
                                google_cse_engine_id="c")).name == "google_cse"
    assert get_provider(Secrets(search_provider="brave", brave_api_key="k")).name == "brave"
    with pytest.raises(SearchError):
        get_provider(Secrets(search_provider="bing"))
    with pytest.raises(SearchError):
        get_provider(Secrets(search_provider="google_cse"))  # missing credentials
