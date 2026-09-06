"""Playwright fetch plus hidden-JSON extraction.

Public pages only, no login, no API key. PRD section 3 rules out driving a
logged-in session: Threads' native search is only exposed to authenticated
mobile clients, and automating a real account is the fastest way to lose it.

Two entry points (profile and single post) over one parser, per R3.
"""

from __future__ import annotations

import asyncio
import logging
import random
from types import TracebackType
from typing import Any

from playwright.async_api import Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeout, async_playwright

from .parser import Post, parse_posts, parse_single_post

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]


class ScrapeError(Exception):
    """A fetch failed in a way worth counting against the source's health."""


class RateLimited(ScrapeError):
    """403 or 429. The caller backs off rather than retrying immediately."""


class NotAvailable(ScrapeError):
    """Private, deleted or otherwise gone. Never worth retrying."""


class ThreadsScraper:
    """Async context manager owning one headless browser for a whole cycle."""

    def __init__(self, headless: bool = True, timeout_seconds: int = 45,
                 chromium_path: str = "") -> None:
        self.headless = headless
        self.timeout_ms = timeout_seconds * 1000
        self.chromium_path = chromium_path
        self._playwright: Any = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> "ThreadsScraper":
        await self.start()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        launch_args: dict[str, Any] = {
            "headless": self.headless,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if self.chromium_path:
            launch_args["executable_path"] = self.chromium_path
        self._browser = await self._playwright.chromium.launch(**launch_args)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _fetch_html(self, url: str) -> str:
        if self._browser is None:
            raise ScrapeError("scraper not started")
        context = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="id-ID",
            viewport={"width": 1280, "height": 1200},
        )
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            status = response.status if response else 0
            if status in (403, 429):
                raise RateLimited(f"{status} for {url}")
            if status in (404, 410):
                raise NotAvailable(f"{status} for {url}")
            if status >= 500:
                raise ScrapeError(f"{status} for {url}")
            # The payload we want is inlined in the document, so there is no
            # need to wait for the full render; a short settle is enough for
            # the deferred data-sjs scripts to land.
            try:
                await page.wait_for_selector('script[type="application/json"]', timeout=10_000)
            except PlaywrightTimeout:
                log.debug("no application/json script appeared for %s", url)
            await page.wait_for_timeout(1500)
            return await page.content()
        except (RateLimited, NotAvailable, ScrapeError):
            raise
        except PlaywrightTimeout as exc:
            raise ScrapeError(f"timeout loading {url}: {exc}") from exc
        except PlaywrightError as exc:
            raise ScrapeError(f"playwright error on {url}: {exc}") from exc
        finally:
            await page.close()
            await context.close()

    async def fetch_profile(self, username: str, limit: int | None = None) -> list[Post]:
        """Recent posts from a public profile (R1)."""
        handle = username.lstrip("@").lower()
        html = await self._fetch_html(f"https://www.threads.net/@{handle}")
        posts = [p for p in parse_posts(html, username_hint=handle) if p.username == handle]
        if not posts:
            # Distinguishing "private/deleted" from "Meta changed the JSON" is
            # not possible from here, so both surface as a zero-result cycle
            # and the loop's zero-streak alerting catches the second case.
            log.warning("no posts parsed for @%s", handle)
        return posts[: limit or len(posts)]

    async def fetch_post(self, url: str) -> Post | None:
        """One post by URL (R3). Returns None when the post is gone."""
        html = await self._fetch_html(url)
        return parse_single_post(html, url)


async def polite_delay(bounds: tuple[int, int]) -> float:
    """Randomised pause between requests. Conservative on purpose (R10)."""
    low, high = bounds
    seconds = random.uniform(low, high)
    await asyncio.sleep(seconds)
    return seconds
