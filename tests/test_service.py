"""Exercise orchestration and persistence while replacing all external I/O."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from webchangesentinel import service
from webchangesentinel.capture import FetchedPage
from webchangesentinel.config import AppConfig, FilterConfig, MonitorConfig, NotificationConfig
from webchangesentinel.notifications import DeliveryResult
from webchangesentinel.service import Sentinel


def monitor(identifier="product", **updates):
    values = {
        "id": identifier,
        "url": f"https://example.com/{identifier}",
        "selector_type": "full",
        "filters": FilterConfig(threshold_percent=50),
    }
    return MonitorConfig(**(values | updates))


@pytest.fixture
def make_sentinel(tmp_path):
    instances = []

    def factory(monitors=None, **updates):
        values = {
            "database_url": f"sqlite:///{tmp_path / f'sentinel-{len(instances)}.db'}",
            "snapshot_dir": tmp_path / "screenshots",
            "monitors": monitors if monitors is not None else [monitor()],
        }
        instance = Sentinel(AppConfig(**(values | updates)))
        instances.append(instance)
        return instance

    yield factory
    for instance in instances:
        instance.close()


@pytest.mark.asyncio
async def test_baseline_unchanged_changed_filtered_preserves_each_snapshot(
    monkeypatch, make_sentinel
):
    app = make_sentinel(
        [monitor(channels=["desktop"])],
        notifications={"desktop": NotificationConfig(kind="desktop")},
    )
    capture = AsyncMock(
        side_effect=[FetchedPage(f"<p>{text}</p>") for text in ["abcd", "abcd", "wxyz", "wxyq"]]
    )
    monkeypatch.setattr(service, "fetch", capture)
    app.dispatcher.send = AsyncMock(return_value=[DeliveryResult("desktop", True)])

    outcomes = [await app.check("product") for _ in range(4)]

    assert [item.status for item in outcomes] == ["baseline", "unchanged", "changed", "filtered"]
    snapshots = app.store.history("product")
    assert len(snapshots) == 4
    assert [item["text"] for item in snapshots] == ["wxyq", "wxyz", "abcd", "abcd"]
    events = app.store.events("product")
    assert [item["status"] for item in events] == ["filtered", "changed"]
    assert events[0]["reason"] == "below_threshold"
    assert events[0]["previous_snapshot_id"] == outcomes[2].snapshot_id
    assert events[1]["previous_snapshot_id"] == outcomes[1].snapshot_id
    assert events[1]["difference_percent"] == 100
    assert "-abcd" in events[1]["diff"] and "+wxyz" in events[1]["diff"]
    app.dispatcher.send.assert_awaited_once()
    sent_alert, channels = app.dispatcher.send.call_args.args
    assert channels == ["desktop"]
    assert sent_alert.monitor_id == "product"
    assert sent_alert.difference_percent == 100
    state = app.store.list_monitors()[0]
    assert state["check_count"] == 4
    assert state["status"] == "filtered"
    assert state["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_delivery_errors_are_persisted_without_erasing_change(monkeypatch, make_sentinel):
    app = make_sentinel(
        [monitor(channels=["good", "bad"])],
        notifications={name: NotificationConfig(kind="desktop") for name in ["good", "bad"]},
    )
    monkeypatch.setattr(
        service,
        "fetch",
        AsyncMock(side_effect=[FetchedPage("<p>abcd</p>"), FetchedPage("<p>wxyz</p>")]),
    )
    app.dispatcher.send = AsyncMock(
        return_value=[
            DeliveryResult("good", True),
            DeliveryResult("bad", False, "Provider unavailable"),
        ]
    )

    await app.check("product")
    outcome = await app.check("product")

    assert outcome.status == "changed"
    assert outcome.delivery_errors == ["bad"]
    assert app.store.events("product")[0]["deliveries"] == [
        {"channel": "good", "success": True, "error": None},
        {"channel": "bad", "success": False, "error": "Provider unavailable"},
    ]
    assert app.store.list_monitors()[0]["consecutive_failures"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_error",
    [
        ("storage", "Could not persist notification delivery results"),
        ("dispatch", "Notification dispatch failed"),
    ],
)
async def test_notification_adapter_failure_keeps_event_and_does_not_abort_check_all(
    monkeypatch, make_sentinel, caplog, failure, expected_error
):
    app = make_sentinel(
        [monitor("first", channels=["desktop"]), monitor("second")],
        notifications={"desktop": NotificationConfig(kind="desktop")},
    )
    texts = {"first": "abcd", "second": "stable"}

    async def capture(current, _):
        return FetchedPage(f"<p>{texts[current.id]}</p>")

    monkeypatch.setattr(service, "fetch", AsyncMock(side_effect=capture))
    baseline = await app.check_all()
    texts["first"] = "wxyz"
    secret = "provider-or-database-secret-do-not-print"
    if failure == "storage":
        dispatch = AsyncMock(return_value=[DeliveryResult("desktop", True)])
        save_deliveries = Mock(side_effect=RuntimeError(secret))
    else:
        dispatch = AsyncMock(side_effect=RuntimeError(secret))
        save_deliveries = Mock(wraps=app.store.record_deliveries)
    app.dispatcher.send = dispatch
    monkeypatch.setattr(app.store, "record_deliveries", save_deliveries)

    outcomes = await app.check_all()

    assert [item.status for item in outcomes] == ["changed", "unchanged"]
    assert outcomes[0].error == expected_error
    assert outcomes[1].error is None
    assert outcomes[0].snapshot_id is not None
    assert app.store.snapshot(outcomes[0].snapshot_id)["text"] == "wxyz"
    assert len(app.store.history("first")) == len(app.store.history("second")) == 2
    events = app.store.events("first")
    assert len(events) == 1
    assert events[0]["status"] == "changed"
    assert events[0]["previous_snapshot_id"] == baseline[0].snapshot_id
    assert events[0]["snapshot_id"] == outcomes[0].snapshot_id
    assert events[0]["deliveries"] == []
    dispatch.assert_awaited_once()
    if failure == "storage":
        save_deliveries.assert_called_once_with(
            events[0]["id"], [{"channel": "desktop", "success": True, "error": None}]
        )
    else:
        save_deliveries.assert_not_called()
    assert secret not in outcomes[0].error + caplog.text
    assert all(state["consecutive_failures"] == 0 for state in app.store.list_monitors())


@pytest.mark.asyncio
async def test_visual_only_change_persists_event_and_reaches_notification(
    monkeypatch, make_sentinel
):
    app = make_sentinel(
        [monitor(visual=True, channels=["desktop"])],
        notifications={"desktop": NotificationConfig(kind="desktop")},
    )
    monkeypatch.setattr(
        service,
        "fetch",
        AsyncMock(
            side_effect=[
                FetchedPage("<p>Same text</p>", image_hash="0000000000000000"),
                FetchedPage("<p>Same text</p>", image_hash="ffffffffffffffff"),
            ]
        ),
    )
    app.dispatcher.send = AsyncMock(return_value=[DeliveryResult("desktop", True)])
    await app.check("product")

    outcome = await app.check("product")

    assert outcome.status == "changed"
    assert outcome.difference_percent == 0
    event = app.store.events("product")[0]
    assert event["difference_percent"] == 0
    assert event["visual_difference_percent"] == 100
    assert event["diff"] == ""
    sent_alert = app.dispatcher.send.call_args.args[0]
    assert sent_alert.visual_difference_percent == 100
    assert sent_alert.difference_percent == 0


@pytest.mark.asyncio
async def test_failed_fetch_keeps_baseline_tracks_failures_and_recovers(
    monkeypatch, make_sentinel, caplog
):
    app = make_sentinel()
    secret = "signed-token-do-not-log"
    monkeypatch.setattr(
        service,
        "fetch",
        AsyncMock(
            side_effect=[
                FetchedPage("<p>abcd</p>"),
                RuntimeError(secret),
                RuntimeError(secret),
                FetchedPage("<p>abcd</p>"),
            ]
        ),
    )
    app.dispatcher.send = AsyncMock()
    baseline = await app.check("product")
    last_success = app.store.list_monitors()[0]["last_success"]

    first_failure = await app.check("product")
    second_failure = await app.check("product")

    assert first_failure.status == second_failure.status == "error"
    assert first_failure.snapshot_id is None
    assert len(app.store.history("product")) == 1
    assert app.store.latest("product")["id"] == baseline.snapshot_id
    assert app.store.events("product") == []
    state = app.store.list_monitors()[0]
    assert state["consecutive_failures"] == 2
    assert state["check_count"] == 3
    assert state["last_success"] == last_success
    assert state["last_error"] == first_failure.error
    assert secret not in caplog.text + first_failure.error

    recovery = await app.check("product")
    assert recovery.status == "unchanged"
    state = app.store.list_monitors()[0]
    assert state["consecutive_failures"] == 0
    assert state["last_error"] is None
    assert state["check_count"] == 4
    app.dispatcher.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_extraction_removes_unpersisted_screenshot(
    monkeypatch, make_sentinel, tmp_path
):
    app = make_sentinel([monitor(selector_type="css", selector="#price")])
    screenshot = tmp_path / "unpersisted.png"
    screenshot.write_bytes(b"mock screenshot")
    monkeypatch.setattr(
        service, "fetch", AsyncMock(return_value=FetchedPage("<p>Wrong page</p>", str(screenshot)))
    )

    result = await app.check("product")

    assert result.status == "error"
    assert "ExtractionError" in result.error
    assert not screenshot.exists()
    assert app.store.latest("product") is None
    assert app.store.history("product") == []
    assert app.store.list_monitors()[0]["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_same_monitor_in_progress_returns_busy_without_duplicate_fetch(
    monkeypatch, make_sentinel
):
    app = make_sentinel()
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*_):
        entered.set()
        await release.wait()
        return FetchedPage("<p>abcd</p>")

    capture = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(service, "fetch", capture)
    running = asyncio.create_task(app.check("product"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        duplicate = await app.check("product")
        assert duplicate.status == "busy"
        assert duplicate.snapshot_id is None
        capture.assert_awaited_once()
    finally:
        release.set()
        completed = await running

    assert completed.status == "baseline"
    assert len(app.store.history("product")) == 1
    assert app.store.list_monitors()[0]["check_count"] == 1


@pytest.mark.asyncio
async def test_check_all_skips_disabled_monitors_and_obeys_concurrency(monkeypatch, make_sentinel):
    app = make_sentinel(
        [monitor("first"), monitor("second"), monitor("third"), monitor("disabled", enabled=False)],
        concurrency=2,
    )
    at_capacity, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    visited = []

    async def fetch(current, _):
        nonlocal active, peak
        visited.append(current.id)
        active += 1
        peak = max(peak, active)
        if active == 2:
            at_capacity.set()
        try:
            await release.wait()
            return FetchedPage("<p>abcd</p>")
        finally:
            active -= 1

    monkeypatch.setattr(service, "fetch", fetch)
    running = asyncio.create_task(app.check_all())
    try:
        await asyncio.wait_for(at_capacity.wait(), timeout=2)
        assert len(visited) == 2
    finally:
        release.set()
        outcomes = await running

    assert peak == 2
    assert set(visited) == {"first", "second", "third"}
    assert [item.monitor_id for item in outcomes] == ["first", "second", "third"]
    assert all(item.status == "baseline" for item in outcomes)
    assert app.store.history("disabled") == []


@pytest.mark.asyncio
async def test_cancelled_fetch_releases_lock_without_recording_failure(monkeypatch, make_sentinel):
    app = make_sentinel()
    entered = asyncio.Event()

    async def blocked_fetch(*_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "fetch", blocked_fetch)
    running = asyncio.create_task(app.check("product"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert app.store.list_monitors()[0]["check_count"] == 0
    monkeypatch.setattr(service, "fetch", AsyncMock(return_value=FetchedPage("<p>abcd</p>")))
    assert (await app.check("product")).status == "baseline"


@pytest.mark.asyncio
async def test_reload_archives_removed_monitor_without_deleting_history(monkeypatch, make_sentinel):
    app = make_sentinel()
    monkeypatch.setattr(service, "fetch", AsyncMock(return_value=FetchedPage("<p>abcd</p>")))
    initial = await app.check("product")
    app.reload_config(app.config.model_copy(update={"monitors": []}))

    assert app.store.list_monitors() == []
    assert app.store.snapshot(initial.snapshot_id)["text"] == "abcd"
    assert len(app.store.history("product")) == 1
    with pytest.raises(KeyError):
        await app.check("product")

    app.reload_config(app.config.model_copy(update={"monitors": [monitor()]}))
    assert (await app.check("product")).status == "unchanged"
    assert app.store.list_monitors()[0]["check_count"] == 2


@pytest.mark.asyncio
async def test_changing_target_url_creates_new_baseline_then_stable_comparison(
    monkeypatch, make_sentinel
):
    app = make_sentinel()
    monkeypatch.setattr(
        service,
        "fetch",
        AsyncMock(
            side_effect=[
                FetchedPage("<p>Old target</p>"),
                FetchedPage("<p>New target</p>"),
                FetchedPage("<p>New target</p>"),
            ]
        ),
    )
    delivery = AsyncMock()
    app.dispatcher.send = delivery
    original = await app.check("product")
    replacement = monitor(url="https://example.com/replacement")
    app.reload_config(app.config.model_copy(update={"monitors": [replacement]}))
    app.dispatcher.send = delivery

    baseline = await app.check("product")
    unchanged = await app.check("product")

    assert baseline.status == "baseline"
    assert unchanged.status == "unchanged"
    assert app.store.snapshot(original.snapshot_id)["text"] == "Old target"
    assert app.store.snapshot(baseline.snapshot_id)["text"] == "New target"
    assert len(app.store.history("product")) == 3
    assert app.store.events("product") == []
    delivery.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["url", "notifications", "remove"])
async def test_reload_during_capture_discards_result_and_never_alerts(
    monkeypatch, make_sentinel, tmp_path, change
):
    app = make_sentinel(
        [monitor(channels=["desktop"])],
        notifications={"desktop": NotificationConfig(kind="desktop")},
    )
    monkeypatch.setattr(service, "fetch", AsyncMock(return_value=FetchedPage("<p>abcd</p>")))
    baseline = await app.check("product")
    old_delivery = AsyncMock()
    app.dispatcher.send = old_delivery
    entered, release = asyncio.Event(), asyncio.Event()
    screenshot = tmp_path / f"stale-{change}.png"

    async def blocked_fetch(*_):
        entered.set()
        await release.wait()
        screenshot.write_bytes(b"mock screenshot")
        return FetchedPage("<p>wxyz</p>", screenshot_path=str(screenshot))

    monkeypatch.setattr(service, "fetch", blocked_fetch)
    running = asyncio.create_task(app.check("product"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if change == "url":
            updates = {"monitors": [monitor(url="https://example.com/new", channels=["desktop"])]}
        elif change == "notifications":
            updates = {
                "notifications": {
                    "desktop": NotificationConfig(kind="desktop"),
                    "new": NotificationConfig(kind="desktop"),
                }
            }
        else:
            updates = {"monitors": []}
        app.reload_config(app.config.model_copy(update=updates))
        new_delivery = AsyncMock()
        app.dispatcher.send = new_delivery
    finally:
        release.set()
        outcome = await running

    assert outcome.status == "busy"
    assert "Configuration changed" in outcome.error
    assert outcome.snapshot_id is None
    assert len(app.store.history("product")) == 1
    assert app.store.latest("product")["id"] == baseline.snapshot_id
    assert app.store.events("product") == []
    assert not screenshot.exists()
    old_delivery.assert_not_awaited()
    new_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_failure_returns_sanitized_error_even_when_failure_logging_fails(
    monkeypatch, make_sentinel, tmp_path, caplog
):
    app = make_sentinel()
    screenshot = tmp_path / "database-failure.png"
    screenshot.write_bytes(b"mock screenshot")
    monkeypatch.setattr(
        service, "fetch", AsyncMock(return_value=FetchedPage("<p>abcd</p>", str(screenshot)))
    )
    monkeypatch.setattr(
        app.store, "record_success", Mock(side_effect=RuntimeError("db-secret-value"))
    )
    monkeypatch.setattr(
        app.store, "record_failure", Mock(side_effect=RuntimeError("failure-secret-value"))
    )

    outcome = await app.check("product")

    assert outcome.status == "error"
    assert outcome.snapshot_id is None
    assert not screenshot.exists()
    assert app.store.history("product") == []
    assert "db-secret-value" not in outcome.error + caplog.text
    assert "failure-secret-value" not in outcome.error + caplog.text
    assert "could not persist failure" in caplog.text


@pytest.mark.asyncio
async def test_unknown_monitor_fails_before_fetch(monkeypatch, make_sentinel):
    app = make_sentinel()
    fetch = AsyncMock()
    monkeypatch.setattr(service, "fetch", fetch)
    with pytest.raises(KeyError, match="unknown"):
        await app.check("unknown")
    fetch.assert_not_awaited()


@pytest.mark.parametrize("update", [{"database_url": "sqlite:///:memory:"}, {"concurrency": 12}])
def test_reload_requires_restart_for_database_or_concurrency(make_sentinel, update):
    app = make_sentinel()
    previous = app.config
    with pytest.raises(ValueError, match="restart"):
        app.reload_config(app.config.model_copy(update=update))
    assert app.config is previous
