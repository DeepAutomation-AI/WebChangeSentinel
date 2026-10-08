"""Application orchestration; capture and delivery are replaceable adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .capture import fetch
from .config import AppConfig
from .detection import compare, extract_content
from .notifications import Alert, NotificationDispatcher
from .storage import Store

logger = logging.getLogger(__name__)


@dataclass
class CheckOutcome:
    monitor_id: str
    status: str
    snapshot_id: int | None = None
    difference_percent: float = 0.0
    error: str | None = None
    delivery_errors: list[str] = field(default_factory=list)


class Sentinel:
    def __init__(self, config: AppConfig):
        self.config = config
        self.store = Store(config.database_url)
        self.store.sync_monitors(config.monitors)
        self.dispatcher = NotificationDispatcher(config.notifications)
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(config.concurrency)

    def reload_config(self, config: AppConfig) -> None:
        if (
            config.database_url != self.config.database_url
            or config.concurrency != self.config.concurrency
            or config.snapshot_dir != self.config.snapshot_dir
        ):
            raise ValueError(
                "Changing the database, snapshot directory or concurrency requires a process restart"
            )
        self.store.sync_monitors(config.monitors)
        self.config = config
        self.dispatcher = NotificationDispatcher(config.notifications)

    async def check(self, monitor_id: str) -> CheckOutcome:
        monitor = next((item for item in self.config.monitors if item.id == monitor_id), None)
        if monitor is None:
            raise KeyError(monitor_id)
        lock = self._locks.setdefault(monitor_id, asyncio.Lock())
        if lock.locked():
            return CheckOutcome(monitor_id=monitor_id, status="busy")
        async with lock, self._semaphore:
            check_config = self.config
            dispatcher = self.dispatcher
            captured = None
            persisted = False
            try:
                captured = await fetch(monitor, check_config.snapshot_dir)
                current = next(
                    (item for item in self.config.monitors if item.id == monitor_id), None
                )
                if current != monitor or self.config.notifications != check_config.notifications:
                    if captured.screenshot_path:
                        Path(captured.screenshot_path).unlink(missing_ok=True)
                    return CheckOutcome(
                        monitor_id,
                        "busy",
                        error="Configuration changed during capture; retry the check",
                    )
                extracted = extract_content(captured.html, monitor)
                previous = self.store.latest(monitor_id)
                identity = {
                    "url": monitor.url,
                    "selector": monitor.selector,
                    "selector_type": monitor.selector_type,
                    "engine": monitor.engine,
                    "visual": monitor.visual,
                    "image_hash": monitor.image_hash,
                    "ignore_selectors": monitor.filters.ignore_selectors,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(identity, sort_keys=True).encode()
                ).hexdigest()
                if previous and previous.get("config_fingerprint") != fingerprint:
                    previous = None
                change = (
                    compare(
                        previous["text"],
                        extracted.text,
                        monitor.filters,
                        previous["image_hash"],
                        captured.image_hash,
                    )
                    if previous
                    else None
                )
                status = (
                    "baseline"
                    if previous is None
                    else (
                        "unchanged"
                        if not change.changed
                        else ("changed" if change.qualifying else "filtered")
                    )
                )
                snapshot_id, event_id = self.store.record_success(
                    monitor_id,
                    extracted,
                    captured,
                    status,
                    previous,
                    change,
                    fingerprint=fingerprint,
                )
                persisted = True
            except asyncio.CancelledError:
                if captured and captured.screenshot_path and not persisted:
                    Path(captured.screenshot_path).unlink(missing_ok=True)
                raise
            except Exception as exc:
                if captured and captured.screenshot_path and not persisted:
                    Path(captured.screenshot_path).unlink(missing_ok=True)
                # External exceptions may include signed URLs, proxy auth or SQL credentials.
                error = (
                    f"Check failed ({type(exc).__name__}); inspect configuration and connectivity"
                )
                logger.warning("Monitor %s: %s", monitor_id, error)
                try:
                    self.store.record_failure(monitor_id, error)
                except Exception:
                    logger.error("Monitor %s: could not persist failure state", monitor_id)
                return CheckOutcome(monitor_id=monitor_id, status="error", error=error)
            outcome = CheckOutcome(
                monitor_id, status, snapshot_id, change.difference_percent if change else 0
            )
            if change and change.qualifying:
                try:
                    results = await dispatcher.send(
                        Alert(
                            monitor.id,
                            monitor.name or monitor.id,
                            monitor.url,
                            change.difference_percent,
                            change.diff,
                            change.visual_difference_percent,
                        ),
                        monitor.channels,
                    )
                except Exception:
                    outcome.error = "Notification dispatch failed"
                    logger.error("Monitor %s: %s", monitor_id, outcome.error)
                else:
                    deliveries = [asdict(result) for result in results]
                    outcome.delivery_errors = [
                        result.channel for result in results if not result.success
                    ]
                    if event_id is not None:
                        try:
                            self.store.record_deliveries(event_id, deliveries)
                        except Exception:
                            outcome.error = "Could not persist notification delivery results"
                            logger.error("Monitor %s: %s", monitor_id, outcome.error)
            logger.info("Monitor %s: %s", monitor_id, status)
            return outcome

    async def check_all(self) -> list[CheckOutcome]:
        return list(
            await asyncio.gather(
                *(self.check(monitor.id) for monitor in self.config.monitors if monitor.enabled)
            )
        )

    def close(self) -> None:
        self.store.close()
