"""Entrypoint.

  python -m src.main                 run forever (systemd/PM2 target)
  python -m src.main --once all      one cycle of each enabled source
  python -m src.main --once source_a one cycle of the watch-list poller
  python -m src.main --dry-run       print notifications instead of sending
  python -m src.main --check         validate config and exit
  python -m src.main --stats         lead counts by source and status
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import InlineKeyboardMarkup

from .config import Config, ConfigError, load_config
from .loop import MonitorLoop, SourceRunner
from .notify import Notifier, build_application
from .pipeline import Pipeline
from .search_api import SearchError, get_provider
from .sources.base import Source
from .sources.profiles import ProfileSource
from .sources.search import SearchSource
from .store import Store

log = logging.getLogger("threads_monitor")


def setup_logging(config: Config) -> None:
    level = getattr(logging, config.runtime.log_level, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.runtime.log_file:
        path = Path(config.runtime.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # These two are chatty at DEBUG and say nothing useful.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


class DryRunNotifier(Notifier):
    """Prints what would be sent. Useful before the Telegram bot exists."""

    async def _send(self, chat_id: str, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        print("\n" + "=" * 60)
        print(f"[dry-run -> {chat_id or 'unset chat'}]")
        print(text)
        if keyboard:
            labels = [b.text for row in keyboard.inline_keyboard for b in row]
            print(f"[buttons: {', '.join(labels)}]")


def build_sources(config: Config, store: Store) -> list[Source]:
    sources: list[Source] = [ProfileSource(config, store)]
    provider = None
    if config.source_b.enabled:
        try:
            provider = get_provider(config.secrets)
        except SearchError as exc:
            # Source B being unconfigured is not a reason to stop Source A.
            log.warning("source_b disabled: %s", exc)
        if provider is None:
            log.warning("source_b enabled in config but no search provider configured")
    sources.append(SearchSource(config, store, provider))
    return sources


def report_config(config: Config) -> None:
    secrets = config.secrets
    provider = "none"
    try:
        found = get_provider(secrets)
        provider = found.name if found else "none"
    except SearchError as exc:
        provider = f"invalid ({exc})"

    print("Config OK")
    print(f"  config file:     {config.path}")
    print(f"  watched accounts: {len(config.source_a.watched_accounts)} "
          f"(source_a {'on' if config.source_a.enabled else 'off'}, "
          f"every {config.source_a.poll_interval_minutes} min)")
    print(f"  search queries:   {len(config.source_b.queries)} "
          f"(source_b {'on' if config.source_b.enabled else 'off'}, "
          f"every {config.source_b.search_interval_hours}h, provider {provider})")
    print(f"  include keywords: {len(config.filters.include)}")
    print(f"  exclude keywords: {len(config.filters.exclude)}")
    print(f"  database:         {config.runtime.database}")
    print(f"  telegram token:   {'set' if secrets.telegram_bot_token else 'MISSING'}")
    print(f"  telegram chat id: {'set' if secrets.telegram_chat_id else 'MISSING'}")
    template = Path(config.drafts.template)
    print(f"  draft template:   {'ok' if template.exists() else 'MISSING'} ({template})")
    if config.seeker.name in ("", "Nama Kamu"):
        print("  note: seeker.name is still the placeholder; drafts will look wrong.")


def report_stats(store: Store) -> None:
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    overall = store.lead_stats()
    week = store.lead_stats(since=week_ago)
    print(f"Leads all time: {overall.get('total', 0)}")
    for key in ("source_a", "source_b", "applied", "not_relevant", "new"):
        if key in overall:
            print(f"  {key:<13} {overall[key]}")
    print(f"Leads last 7 days: {week.get('total', 0)}")
    for key in ("source_a", "source_b", "applied", "not_relevant"):
        if key in week:
            print(f"  {key:<13} {week[key]}")
    print(f"Posts seen: {store.count_posts()}")
    for source in ("source_a", "source_b"):
        state = store.get_source_state(source)
        print(f"{source}: failures={state.consecutive_failures} "
              f"zero_streak={state.zero_result_streak} "
              f"paused={'yes' if state.is_paused() else 'no'} "
              f"last_ok={state.last_success_at or 'never'}")
    filtered = store.recent_filtered(limit=5)
    if filtered:
        print("Recently filtered (review these weekly):")
        for row in filtered:
            print(f"  @{row['username']}: {row['reason']}")


async def run_once(config: Config, store: Store, which: str, dry_run: bool) -> None:
    notifier = (DryRunNotifier if dry_run else Notifier)(config, store)
    pipeline = Pipeline(config, store, notifier)
    sources = build_sources(config, store)
    ran = False
    for source in sources:
        if which not in ("all", source.name):
            continue
        if not source.enabled:
            log.warning("%s is not enabled, skipping", source.name)
            continue
        ran = True
        await SourceRunner(source, config, store, pipeline, notifier).run_cycle()
    if not ran:
        log.warning("nothing ran for --once %s", which)


async def run_forever(config: Config, store: Store, dry_run: bool) -> None:
    notifier = (DryRunNotifier if dry_run else Notifier)(config, store)
    pipeline = Pipeline(config, store, notifier)
    monitor = MonitorLoop(config, store, pipeline, notifier, build_sources(config, store))
    if not monitor.runners:
        log.error("no sources enabled, nothing to do")
        return

    # The Application only exists to receive button presses; polling it in the
    # same event loop as the scheduler keeps the whole bot one process.
    application = None if dry_run else build_application(config, store)
    stopping = asyncio.Event()

    def request_stop() -> None:
        log.info("shutdown requested")
        stopping.set()

    running_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            running_loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    monitor.schedule()
    monitor.start()
    if application is not None:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
    log.info("running; %d source(s) scheduled", len(monitor.runners))

    try:
        await stopping.wait()
    finally:
        monitor.shutdown()
        if application is not None:
            if application.updater.running:
                await application.updater.stop()
            await application.stop()
            await application.shutdown()
        log.info("stopped")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="threads-job-monitor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--once", choices=["all", "source_a", "source_b"],
                        help="run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print notifications instead of sending them")
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    parser.add_argument("--stats", action="store_true", help="print lead counts and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config, args.env)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        report_config(config)
        return 0

    setup_logging(config)
    store = Store(config.runtime.database)
    try:
        if args.stats:
            report_stats(store)
            return 0
        if args.once:
            asyncio.run(run_once(config, store, args.once, args.dry_run))
        else:
            asyncio.run(run_forever(config, store, args.dry_run))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
