"""Search providers behind one interface.

Whichever provider you pick will eventually annoy you (PRD section 7), so
swapping is a one-file change: implement SearchProvider, add it to
get_provider, done. Nothing above this module knows which one is in use.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

log = logging.getLogger(__name__)


class SearchError(Exception):
    """The search call failed. Counts against Source B's health, not Source A's."""


class QuotaExhausted(SearchError):
    """Provider says the daily allowance is gone. Stop cleanly, resume tomorrow."""


class SearchProvider(ABC):
    name = "abstract"
    #: Roughly how many result URLs one API call can return.
    page_size = 10

    @abstractmethod
    async def search(self, query: str, limit: int, date_restrict: str) -> list[str]:
        """Result URLs for one query. Raises QuotaExhausted when out of quota."""

    async def aclose(self) -> None:
        return None


class GoogleCSEProvider(SearchProvider):
    """Google Custom Search JSON API. 100 queries/day on the free tier."""

    name = "google_cse"
    endpoint = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, engine_id: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key or not engine_id:
            raise SearchError("google_cse needs GOOGLE_CSE_API_KEY and GOOGLE_CSE_ENGINE_ID")
        self.api_key = api_key
        self.engine_id = engine_id
        self._client = client or httpx.AsyncClient(timeout=20)

    async def search(self, query: str, limit: int, date_restrict: str) -> list[str]:
        params: dict[str, Any] = {
            "key": self.api_key,
            "cx": self.engine_id,
            "q": query,
            "num": max(1, min(10, limit)),
        }
        if date_restrict:
            params["dateRestrict"] = date_restrict
        try:
            response = await self._client.get(self.endpoint, params=params)
        except httpx.HTTPError as exc:
            raise SearchError(f"google_cse request failed: {exc}") from exc

        if response.status_code == 429:
            raise QuotaExhausted("google_cse returned 429")
        if response.status_code == 403:
            # CSE uses 403 for both quota exhaustion and a bad key; the reason
            # string is the only way to tell, and guessing wrong either burns
            # the day's quota or hides a broken key.
            body = response.text.lower()
            if "quota" in body or "ratelimitexceeded" in body or "dailylimitexceeded" in body:
                raise QuotaExhausted("google_cse daily quota exhausted")
            raise SearchError(f"google_cse 403: {response.text[:200]}")
        if response.status_code >= 400:
            raise SearchError(f"google_cse {response.status_code}: {response.text[:200]}")

        payload = response.json()
        return [item.get("link", "") for item in payload.get("items", []) if item.get("link")]

    async def aclose(self) -> None:
        await self._client.aclose()


class BraveProvider(SearchProvider):
    """Brave Search API. Independent index, more generous free tier."""

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"
    page_size = 20

    #: Brave uses freshness codes rather than Google's dateRestrict.
    FRESHNESS = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise SearchError("brave needs BRAVE_API_KEY")
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=20)

    def _freshness(self, date_restrict: str) -> str:
        if not date_restrict:
            return ""
        return self.FRESHNESS.get(date_restrict[0].lower(), "pw")

    async def search(self, query: str, limit: int, date_restrict: str) -> list[str]:
        params: dict[str, Any] = {"q": query, "count": max(1, min(20, limit))}
        freshness = self._freshness(date_restrict)
        if freshness:
            params["freshness"] = freshness
        headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key}
        try:
            response = await self._client.get(self.endpoint, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise SearchError(f"brave request failed: {exc}") from exc

        if response.status_code == 429:
            raise QuotaExhausted("brave returned 429")
        if response.status_code >= 400:
            raise SearchError(f"brave {response.status_code}: {response.text[:200]}")

        payload = response.json()
        results = (payload.get("web") or {}).get("results") or []
        return [r.get("url", "") for r in results if r.get("url")]

    async def aclose(self) -> None:
        await self._client.aclose()


def get_provider(secrets: Any) -> SearchProvider | None:
    """Build the configured provider, or None when Source B has no search API."""
    choice = (getattr(secrets, "search_provider", "") or "none").lower()
    if choice in ("", "none", "off", "disabled"):
        return None
    if choice in ("google", "google_cse", "cse"):
        return GoogleCSEProvider(secrets.google_cse_api_key, secrets.google_cse_engine_id)
    if choice == "brave":
        return BraveProvider(secrets.brave_api_key)
    raise SearchError(f"unknown SEARCH_PROVIDER: {choice}")
