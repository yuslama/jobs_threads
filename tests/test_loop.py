from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.loop import MonitorLoop, SourceRunner, backoff_minutes
from src.pipeline import Pipeline
from src.sources.base import Source, SourceResult
from tests.test_pipeline import RecordingNotifier, make_post


class FakeSource(Source):
    def __init__(self, name="source_a", results=None, raises=None):
        self.name = name
        self.results = list(results or [])
        self.raises = raises
        self.calls = 0

    @property
    def enabled(self):
        return True

    async def collect(self):
        self.calls += 1
        if self.raises:
            raise self.raises
        if self.results:
            return self.results.pop(0)
        return SourceResult(source=self.name)


@pytest.fixture
def runner_parts(config, store):
    notifier = RecordingNotifier(config, store)
    pipeline = Pipeline(config, store, notifier)

    def build(source):
        return SourceRunner(source, config, store, pipeline, notifier), notifier

    return build


def test_backoff_schedule_doubles_then_holds_at_the_ceiling():
    schedule = [2, 4, 8, 16, 32, 60]
    assert [backoff_minutes(schedule, i) for i in range(6)] == schedule
    assert backoff_minutes(schedule, 6) == 60
    assert backoff_minutes(schedule, 99) == 60
    assert backoff_minutes([], 0) == 0


@pytest.mark.asyncio
async def test_a_successful_cycle_clears_failure_state(config, store, runner_parts):
    source = FakeSource(results=[SourceResult("source_a", posts=[make_post("1", "Loker hiring")])])
    runner, notifier = runner_parts(source)

    await runner.run_cycle()

    state = store.get_source_state("source_a")
    assert state.consecutive_failures == 0
    assert state.zero_result_streak == 0
    assert state.last_success_at is not None
    assert notifier.sent  # the lead went out


@pytest.mark.asyncio
async def test_a_crashing_source_does_not_propagate(config, store, runner_parts):
    runner, _ = runner_parts(FakeSource(raises=RuntimeError("kaboom")))

    await runner.run_cycle()  # must not raise

    state = store.get_source_state("source_a")
    assert state.consecutive_failures == 1
    assert "kaboom" in state.last_error


@pytest.mark.asyncio
async def test_rate_limiting_pauses_the_source_with_growing_backoff(config, store, runner_parts):
    source = FakeSource(results=[
        SourceResult("source_a", fatal="rate limited on @x: 403"),
        SourceResult("source_a", fatal="rate limited on @x: 403"),
    ])
    runner, _ = runner_parts(source)

    await runner.run_cycle()
    first = store.get_source_state("source_a")
    assert first.backoff_level == 1
    assert first.is_paused()

    # Still paused, so the next cycle is skipped entirely.
    await runner.run_cycle()
    assert source.calls == 1

    # Once the pause lapses, the next failure waits longer.
    first.paused_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    store.save_source_state(first)
    await runner.run_cycle()
    second = store.get_source_state("source_a")
    assert second.backoff_level == 2
    assert second.paused_until - datetime.now(timezone.utc) > timedelta(minutes=3)


@pytest.mark.asyncio
async def test_consecutive_failures_alert_yus_once_then_recover(config, store, runner_parts):
    config.reliability.max_consecutive_failures = 3
    source = FakeSource(results=[SourceResult("source_a", fatal="boom") for _ in range(4)])
    runner, notifier = runner_parts(source)

    for _ in range(3):
        state = store.get_source_state("source_a")
        state.paused_until = None
        store.save_source_state(state)
        await runner.run_cycle()

    alerts = [m for m in notifier.sent if "Source problem" in m]
    assert len(alerts) == 1
    assert "source_a" in alerts[0]
    assert store.get_source_state("source_a").is_paused()

    # A later good cycle says so, once.
    state = store.get_source_state("source_a")
    state.paused_until = None
    store.save_source_state(state)
    source.results = [SourceResult("source_a", posts=[make_post("9", "Loker hiring")])]
    await runner.run_cycle()
    assert any("Recovered" in m for m in notifier.sent)
    assert store.get_source_state("source_a").alerted is False


@pytest.mark.asyncio
async def test_silent_zero_result_cycles_raise_an_alert(config, store, runner_parts):
    config.reliability.zero_result_alert_streak = 2
    runner, notifier = runner_parts(FakeSource())

    await runner.run_cycle()
    assert not [m for m in notifier.sent if "zero posts" in m]

    await runner.run_cycle()
    alerts = [m for m in notifier.sent if "zero posts" in m]
    assert len(alerts) == 1

    # It does not repeat every cycle after that.
    await runner.run_cycle()
    assert len([m for m in notifier.sent if "zero posts" in m]) == 1


@pytest.mark.asyncio
async def test_a_planned_quota_stop_is_not_a_silent_failure(config, store, runner_parts):
    config.reliability.zero_result_alert_streak = 1
    runner, notifier = runner_parts(
        FakeSource(name="source_b", results=[SourceResult("source_b", stopped_early=True)])
    )

    await runner.run_cycle()

    assert not [m for m in notifier.sent if "zero posts" in m]
    assert store.get_source_state("source_b").zero_result_streak == 0


@pytest.mark.asyncio
async def test_sources_fail_independently(config, store, runner_parts):
    broken, _ = runner_parts(FakeSource(name="source_b", raises=RuntimeError("search dead")))
    healthy, notifier = runner_parts(
        FakeSource(name="source_a", results=[SourceResult("source_a", posts=[make_post("1", "Loker hiring")])])
    )

    await broken.run_cycle()
    await healthy.run_cycle()

    assert store.get_source_state("source_b").consecutive_failures == 1
    assert store.get_source_state("source_a").consecutive_failures == 0
    assert notifier.sent


def test_scheduler_registers_one_job_per_enabled_source(config, store):
    notifier = RecordingNotifier(config, store)
    pipeline = Pipeline(config, store, notifier)
    monitor = MonitorLoop(config, store, pipeline, notifier,
                          [FakeSource("source_a"), FakeSource("source_b")])
    monitor.schedule()

    job_ids = {job.id for job in monitor.scheduler.get_jobs()}
    assert {"source_a", "source_b", "notify_queue"} <= job_ids
    monitor.shutdown()
