"""Scheduling, per-source backoff, and failure alerting (R10).

Sources fail independently: Source B dying must not stop Source A. Both a
crash and a silent zero-result run are treated as problems worth telling Yus
about, because a scraper that quietly returns nothing is worse than one that
crashes -- nobody notices for a week, and a week is a long time in a job hunt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import Config
from .notify import Notifier
from .pipeline import Pipeline
from .sources.base import Source
from .store import SourceState, Store

log = logging.getLogger(__name__)


def backoff_minutes(schedule: list[int], level: int) -> int:
    """Minutes to wait at a given backoff level, holding at the last step."""
    if not schedule:
        return 0
    return schedule[min(level, len(schedule) - 1)]


class SourceRunner:
    """Runs one source on its own cadence, with its own health record."""

    def __init__(self, source: Source, config: Config, store: Store,
                 pipeline: Pipeline, notifier: Notifier) -> None:
        self.source = source
        self.config = config
        self.store = store
        self.pipeline = pipeline
        self.notifier = notifier

    async def run_cycle(self) -> None:
        """One cycle. Never raises: a source crash must not kill the scheduler."""
        name = self.source.name
        state = self.store.get_source_state(name)

        if state.is_paused():
            log.info("%s paused until %s, skipping cycle", name, state.paused_until)
            return

        log.info("%s: cycle start", name)
        try:
            result = await self.source.collect()
        except Exception as exc:  # noqa: BLE001 - the whole point is to survive
            log.exception("%s: cycle raised", name)
            await self._record_failure(state, str(exc))
            return

        if not result.ok:
            await self._record_failure(state, result.fatal or "unknown failure")
            return

        try:
            stats = await self.pipeline.process(result.posts, name)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: pipeline raised", name)
            await self._record_failure(state, f"pipeline: {exc}")
            return

        log.info("%s: %s%s", name, stats.summary(),
                 f" ({len(result.errors)} non-fatal errors)" if result.errors else "")
        await self._record_success(state, result.posts, stats, result.stopped_early)

    async def _record_failure(self, state: SourceState, detail: str) -> None:
        reliability = self.config.reliability
        state.consecutive_failures += 1
        state.last_error = detail[:500]

        rate_limited = any(code in detail for code in ("403", "429", "rate limited"))
        if rate_limited:
            # 2, 4, 8, 16, 32, 60 minutes, then hold at the ceiling.
            wait = backoff_minutes(reliability.backoff_minutes, state.backoff_level)
            state.backoff_level += 1
            state.paused_until = datetime.now(timezone.utc) + timedelta(minutes=wait)
            log.warning("%s: rate limited, backing off %d min", state.source, wait)

        if state.consecutive_failures >= reliability.max_consecutive_failures:
            pause_until = datetime.now(timezone.utc) + timedelta(minutes=reliability.pause_minutes)
            # Never shorten a rate-limit pause that already runs longer.
            if state.paused_until is None or pause_until > state.paused_until:
                state.paused_until = pause_until
            if not state.alerted:
                await self.notifier.send_source_alert(
                    state.source,
                    f"{state.consecutive_failures} consecutive failed cycles. "
                    f"Paused for {reliability.pause_minutes} min.\nLast error: {detail[:300]}",
                )
                state.alerted = True

        self.store.save_source_state(state)

    async def _record_success(self, state: SourceState, posts: list, stats, stopped_early: bool) -> None:
        reliability = self.config.reliability
        recovered = state.consecutive_failures > 0 or state.alerted
        state.consecutive_failures = 0
        state.backoff_level = 0
        state.paused_until = None
        state.last_error = None
        state.last_success_at = datetime.now(timezone.utc).isoformat()

        if posts:
            state.zero_result_streak = 0
        elif not stopped_early:
            # Quota exhaustion is a planned stop, not a silent failure.
            state.zero_result_streak += 1

        if recovered and state.alerted:
            await self.notifier.send_source_alert(state.source, "Recovered: cycle completed normally.")
            state.alerted = False

        streak = reliability.zero_result_alert_streak
        if streak and state.zero_result_streak == streak:
            # This is the dangerous failure mode: no errors, no posts. Usually
            # it means Meta changed the hidden JSON shape.
            await self.notifier.send_source_alert(
                state.source,
                f"{state.zero_result_streak} consecutive cycles returned zero posts with no "
                f"errors. The page structure may have changed, or the queries have gone dry.",
            )

        self.store.save_source_state(state)


class MonitorLoop:
    """Owns the scheduler and the runners."""

    def __init__(self, config: Config, store: Store, pipeline: Pipeline,
                 notifier: Notifier, sources: list[Source]) -> None:
        self.config = config
        self.store = store
        self.pipeline = pipeline
        self.notifier = notifier
        self.runners = [
            SourceRunner(source, config, store, pipeline, notifier)
            for source in sources
            if source.enabled
        ]
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def schedule(self) -> None:
        intervals = {
            "source_a": IntervalTrigger(minutes=self.config.source_a.poll_interval_minutes),
            "source_b": IntervalTrigger(hours=self.config.source_b.search_interval_hours),
        }
        for runner in self.runners:
            trigger = intervals.get(runner.source.name, IntervalTrigger(hours=1))
            self.scheduler.add_job(
                runner.run_cycle,
                trigger=trigger,
                id=runner.source.name,
                name=f"{runner.source.name} cycle",
                # A slow cycle must not stack up behind itself.
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
            log.info("scheduled %s", runner.source.name)

        self.scheduler.add_job(
            self._flush_notifications,
            trigger=IntervalTrigger(minutes=5),
            id="notify_queue",
            name="retry queued notifications",
            max_instances=1,
            coalesce=True,
        )

    async def _flush_notifications(self) -> None:
        try:
            sent = await self.notifier.flush_queue()
        except Exception:  # noqa: BLE001
            log.exception("notification flush raised")
            return
        if sent:
            log.info("flushed %d queued notification(s)", sent)

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
