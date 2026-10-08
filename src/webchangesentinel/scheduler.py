"""Single-process scheduling; persistent state lives in SQL, jobs derive from YAML."""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import interval_seconds
from .service import Sentinel


class MonitorScheduler:
    def __init__(self, service: Sentinel):
        self.service = service
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def sync_jobs(self) -> None:
        enabled = {
            monitor.id: monitor for monitor in self.service.config.monitors if monitor.enabled
        }
        for job in self.scheduler.get_jobs():
            if job.id not in enabled:
                self.scheduler.remove_job(job.id)
        for monitor in enabled.values():
            seconds = interval_seconds(monitor.interval)
            existing = self.scheduler.get_job(monitor.id)
            if existing and existing.trigger.interval.total_seconds() == seconds:
                continue
            if existing:
                # Pending jobs are not in a jobstore yet: replace_existing alone
                # would leave duplicates when syncing before scheduler.start().
                self.scheduler.remove_job(monitor.id)
            self.scheduler.add_job(
                self.service.check,
                trigger=IntervalTrigger(seconds=seconds, timezone="UTC"),
                args=[monitor.id],
                id=monitor.id,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=max(1, int(min(seconds, 3600))),
            )

    def start(self) -> None:
        self.sync_jobs()
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
