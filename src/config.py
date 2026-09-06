"""YAML + .env configuration loading.

One config file, one user (PRD section 3: do not build a user table). Secrets
come from the environment only, so the config file stays committable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Config is missing something the bot cannot run without."""


@dataclass
class SeekerConfig:
    name: str = ""
    email: str = ""
    phone: str = ""
    portfolio: str = ""


@dataclass
class SourceAConfig:
    enabled: bool = True
    poll_interval_minutes: int = 30
    delay_seconds: tuple[int, int] = (30, 90)
    first_run_max_age_hours: int = 48
    posts_per_profile: int = 12
    watched_accounts: list[str] = field(default_factory=list)


@dataclass
class SourceBConfig:
    enabled: bool = True
    search_interval_hours: int = 6
    scrape_delay_seconds: tuple[int, int] = (60, 150)
    max_scrapes_per_cycle: int = 25
    results_per_query: int = 10
    date_restrict: str = "d2"
    daily_query_quota: int = 100
    queries: list[str] = field(default_factory=list)


@dataclass
class FilterConfig:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    role_titles: list[str] = field(default_factory=list)


@dataclass
class DiscoveryConfig:
    enabled: bool = True
    suggest_after_hits: int = 2


@dataclass
class DraftConfig:
    enabled: bool = True
    template: str = "templates/application_email.txt"


@dataclass
class ReliabilityConfig:
    backoff_minutes: list[int] = field(default_factory=lambda: [2, 4, 8, 16, 32, 60])
    max_consecutive_failures: int = 5
    pause_minutes: int = 60
    zero_result_alert_streak: int = 3
    notify_max_attempts: int = 10


@dataclass
class RuntimeConfig:
    database: str = "data/monitor.db"
    log_level: str = "INFO"
    log_file: str = "logs/monitor.log"
    page_timeout_seconds: int = 45
    headless: bool = True
    #: Optional explicit Chromium binary. Set it when the VPS already has a
    #: system Chromium, or when Playwright's bundled build is not where it
    #: expects. Empty means "let Playwright find its own".
    chromium_path: str = ""


@dataclass
class Secrets:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_operator_chat_id: str = ""
    search_provider: str = "none"
    google_cse_api_key: str = ""
    google_cse_engine_id: str = ""
    brave_api_key: str = ""

    @property
    def operator_chat_id(self) -> str:
        return self.telegram_operator_chat_id or self.telegram_chat_id


@dataclass
class Config:
    seeker: SeekerConfig
    source_a: SourceAConfig
    source_b: SourceBConfig
    filters: FilterConfig
    discovery: DiscoveryConfig
    drafts: DraftConfig
    reliability: ReliabilityConfig
    runtime: RuntimeConfig
    secrets: Secrets
    path: Path | None = None

    def watched(self) -> set[str]:
        """Watch list, normalised for comparison against scraped usernames."""
        return {normalise_username(a) for a in self.source_a.watched_accounts}


def normalise_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


def _pair(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return (int(value), int(value))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        low, high = int(value[0]), int(value[1])
        return (low, high) if low <= high else (high, low)
    raise ConfigError(f"expected a number or a [min, max] pair, got {value!r}")


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"config section '{name}' must be a mapping")
    return section


def load_secrets(env_file: str | Path | None = ".env") -> Secrets:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    return Secrets(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_operator_chat_id=os.getenv("TELEGRAM_OPERATOR_CHAT_ID", "").strip(),
        search_provider=os.getenv("SEARCH_PROVIDER", "none").strip().lower() or "none",
        google_cse_api_key=os.getenv("GOOGLE_CSE_API_KEY", "").strip(),
        google_cse_engine_id=os.getenv("GOOGLE_CSE_ENGINE_ID", "").strip(),
        brave_api_key=os.getenv("BRAVE_API_KEY", "").strip(),
    )


def load_config(path: str | Path = "config.yaml", env_file: str | Path | None = ".env") -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config file must be a YAML mapping")

    a = _section(raw, "source_a")
    b = _section(raw, "source_b")
    f = _section(raw, "filters")
    d = _section(raw, "discovery")
    dr = _section(raw, "drafts")
    r = _section(raw, "reliability")
    rt = _section(raw, "runtime")
    s = _section(raw, "seeker")

    config = Config(
        seeker=SeekerConfig(
            name=str(s.get("name", "")),
            email=str(s.get("email", "")),
            phone=str(s.get("phone", "")),
            portfolio=str(s.get("portfolio", "")),
        ),
        source_a=SourceAConfig(
            enabled=bool(a.get("enabled", True)),
            poll_interval_minutes=int(a.get("poll_interval_minutes", 30)),
            delay_seconds=_pair(a.get("delay_seconds"), (30, 90)),
            first_run_max_age_hours=int(a.get("first_run_max_age_hours", 48)),
            posts_per_profile=int(a.get("posts_per_profile", 12)),
            watched_accounts=[str(x) for x in (a.get("watched_accounts") or [])],
        ),
        source_b=SourceBConfig(
            enabled=bool(b.get("enabled", True)),
            search_interval_hours=int(b.get("search_interval_hours", 6)),
            scrape_delay_seconds=_pair(b.get("scrape_delay_seconds"), (60, 150)),
            max_scrapes_per_cycle=int(b.get("max_scrapes_per_cycle", 25)),
            results_per_query=int(b.get("results_per_query", 10)),
            date_restrict=str(b.get("date_restrict", "d2")),
            daily_query_quota=int(b.get("daily_query_quota", 100)),
            queries=[str(x) for x in (b.get("queries") or [])],
        ),
        filters=FilterConfig(
            include=[str(x) for x in (f.get("include") or [])],
            exclude=[str(x) for x in (f.get("exclude") or [])],
            role_titles=[str(x) for x in (f.get("role_titles") or [])],
        ),
        discovery=DiscoveryConfig(
            enabled=bool(d.get("enabled", True)),
            suggest_after_hits=max(1, int(d.get("suggest_after_hits", 2))),
        ),
        drafts=DraftConfig(
            enabled=bool(dr.get("enabled", True)),
            template=str(dr.get("template", "templates/application_email.txt")),
        ),
        reliability=ReliabilityConfig(
            backoff_minutes=[int(x) for x in (r.get("backoff_minutes") or [2, 4, 8, 16, 32, 60])],
            max_consecutive_failures=int(r.get("max_consecutive_failures", 5)),
            pause_minutes=int(r.get("pause_minutes", 60)),
            zero_result_alert_streak=int(r.get("zero_result_alert_streak", 3)),
            notify_max_attempts=int(r.get("notify_max_attempts", 10)),
        ),
        runtime=RuntimeConfig(
            database=str(rt.get("database", "data/monitor.db")),
            log_level=str(rt.get("log_level", "INFO")).upper(),
            log_file=str(rt.get("log_file", "logs/monitor.log")),
            page_timeout_seconds=int(rt.get("page_timeout_seconds", 45)),
            headless=bool(rt.get("headless", True)),
            chromium_path=str(rt.get("chromium_path", "") or os.getenv("CHROMIUM_PATH", "")),
        ),
        secrets=load_secrets(env_file),
        path=path,
    )

    if config.source_a.enabled and not config.source_a.watched_accounts:
        raise ConfigError("source_a is enabled but watched_accounts is empty")
    if not config.filters.include:
        raise ConfigError("filters.include is empty; every post would be dropped")
    return config
