"""Inspect real APScheduler jobs without running network or notification work."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from webchangesentinel.config import AppConfig, MonitorConfig
from webchangesentinel.scheduler import MonitorScheduler


def monitor(identifier, interval="5m", enabled=True):
    return MonitorConfig(
        id=identifier, url=f"https://example.com/{identifier}", interval=interval, enabled=enabled
    )


def make_scheduler(monitors):
    return MonitorScheduler(SimpleNamespace(config=AppConfig(monitors=monitors), check=AsyncMock()))


@pytest.mark.asyncio
async def test_enabled_monitors_have_intervals_and_overlap_protection():
    scheduler = make_scheduler(
        [
            monitor("minutes", "5m"),
            monitor("hours", "2h"),
            monitor("days", "1d"),
            monitor("disabled", enabled=False),
        ]
    )
    scheduler.sync_jobs()
    scheduler.scheduler.start(paused=True)
    try:
        jobs = {job.id: job for job in scheduler.scheduler.get_jobs()}
        assert set(jobs) == {"minutes", "hours", "days"}
        assert {name: job.trigger.interval.total_seconds() for name, job in jobs.items()} == {
            "minutes": 300,
            "hours": 7200,
            "days": 86400,
        }
        for job in jobs.values():
            assert job.args == (job.id,)
            assert job.func == scheduler.service.check
            assert job.max_instances == 1
            assert job.coalesce is True
            assert job.misfire_grace_time >= 1
        scheduler.service.check.assert_not_awaited()
    finally:
        scheduler.stop()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_reloading_jobs_updates_interval_removes_disabled_without_duplicates():
    scheduler = make_scheduler([monitor("first"), monitor("second")])
    scheduler.sync_jobs()
    scheduler.scheduler.start(paused=True)
    try:
        original_job = scheduler.scheduler.get_job("first")
        scheduler.sync_jobs()
        assert scheduler.scheduler.get_job("first") is original_job
        scheduler.service.config = AppConfig(
            monitors=[
                monitor("first", "1h"),
                monitor("second", enabled=False),
                monitor("third", "1d"),
            ]
        )
        scheduler.sync_jobs()
        scheduler.sync_jobs()
        jobs = scheduler.scheduler.get_jobs()
        assert {job.id for job in jobs} == {"first", "third"}
        assert len(jobs) == 2
        assert scheduler.scheduler.get_job("first").trigger.interval.total_seconds() == 3600
        scheduler.service.config = AppConfig(monitors=[])
        scheduler.sync_jobs()
        assert scheduler.scheduler.get_jobs() == []
    finally:
        scheduler.stop()
        await asyncio.sleep(0)


def test_pending_jobs_do_not_duplicate_when_interval_changes_before_start():
    scheduler = make_scheduler([monitor("first", "5m")])
    scheduler.sync_jobs()
    scheduler.service.config = AppConfig(monitors=[monitor("first", "1h")])
    scheduler.sync_jobs()
    scheduler.sync_jobs()

    jobs = scheduler.scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "first"
    assert jobs[0].trigger.interval.total_seconds() == 3600


def test_start_syncs_jobs_and_stop_only_shuts_down_running_scheduler(monkeypatch):
    scheduler = make_scheduler([monitor("first")])
    sync = Mock()
    backend = Mock(running=False)
    monkeypatch.setattr(scheduler, "sync_jobs", sync)
    monkeypatch.setattr(scheduler, "scheduler", backend)

    scheduler.start()
    sync.assert_called_once()
    backend.start.assert_called_once()
    scheduler.stop()
    backend.shutdown.assert_not_called()
    backend.running = True
    scheduler.stop()
    backend.shutdown.assert_called_once_with(wait=False)
