"""Persist snapshots, linked events and monitor state in real temporary SQLite."""

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from webchangesentinel.capture import FetchedPage
from webchangesentinel.config import FilterConfig, MonitorConfig
from webchangesentinel.detection import compare, extract_content
from webchangesentinel.storage import MonitorState, Snapshot, Store


@pytest.fixture
def monitored():
    return MonitorConfig(
        id="product", name="Product", url="https://example.com/product", selector_type="full"
    )


@pytest.fixture
def store(tmp_path, monitored):
    instance = Store(f"sqlite:///{tmp_path / 'nested' / 'sentinel.db'}")
    instance.sync_monitors([monitored])
    yield instance
    instance.close()


def save_snapshot(store, monitored, text, status="baseline", previous=None, change=None):
    html = f"<p>{text}</p><script>ignored()</script>"
    extracted = extract_content(html, monitored)
    captured = FetchedPage(html, "/tmp/mock-screenshot.png", "0000000000000000")
    return store.record_success(monitored.id, extracted, captured, status, previous, change)


def test_clean_html_hash_and_snapshot_metadata_are_persisted(store, monitored):
    snapshot_id, event_id = save_snapshot(store, monitored, "Price 10")

    snapshot = store.snapshot(snapshot_id)
    assert event_id is None
    assert snapshot["text"] == "Price 10"
    assert "ignored" not in snapshot["clean_html"]
    assert len(snapshot["content_hash"]) == 64
    assert snapshot["screenshot_path"] == "/tmp/mock-screenshot.png"
    assert snapshot["image_hash"] == "0000000000000000"
    assert datetime.fromisoformat(snapshot["created_at"]).tzinfo is not None
    assert store.latest(monitored.id)["id"] == snapshot_id
    state = store.list_monitors()[0]
    assert state["status"] == "baseline"
    assert state["check_count"] == 1
    assert state["last_success"] == state["last_checked"]
    assert store.snapshot(987654) is None


def test_changed_and_filtered_events_reference_the_correct_snapshots(store, monitored):
    baseline_id, _ = save_snapshot(store, monitored, "abcd")
    previous = store.latest(monitored.id)
    change = compare("abcd", "wxyz", FilterConfig(threshold_percent=0))
    changed_id, changed_event = save_snapshot(store, monitored, "wxyz", "changed", previous, change)
    previous = store.latest(monitored.id)
    filtered = compare("wxyz", "wxyq", FilterConfig(threshold_percent=50))
    filtered_id, filtered_event = save_snapshot(
        store, monitored, "wxyq", "filtered", previous, filtered
    )
    deliveries = [{"channel": "mail", "success": False, "error": "SMTP unavailable"}]
    store.record_deliveries(changed_event, deliveries)

    events = store.events(monitored.id)
    assert [item["id"] for item in events] == [filtered_event, changed_event]
    assert events[0]["snapshot_id"] == filtered_id
    assert events[0]["previous_snapshot_id"] == changed_id
    assert events[0]["reason"] == "below_threshold"
    assert events[0]["deliveries"] == []
    assert events[1]["previous_snapshot_id"] == baseline_id
    assert events[1]["deliveries"] == deliveries
    assert events[1]["changed_chars"] == 4
    assert events[1]["difference_percent"] == 100


def test_failure_updates_state_without_modifying_last_success_or_history(store, monitored):
    snapshot_id, _ = save_snapshot(store, monitored, "Price 10")
    last_success = store.list_monitors()[0]["last_success"]

    store.record_failure(monitored.id, "Connection failed")
    store.record_failure(monitored.id, "Timeout")

    state = store.list_monitors()[0]
    assert state["status"] == "error"
    assert state["consecutive_failures"] == 2
    assert state["check_count"] == 3
    assert state["last_error"] == "Timeout"
    assert state["last_success"] == last_success
    assert len(store.history(monitored.id)) == 1
    assert store.latest(monitored.id)["id"] == snapshot_id
    assert store.events(monitored.id) == []

    previous = store.latest(monitored.id)
    save_snapshot(
        store,
        monitored,
        "Price 10",
        "unchanged",
        previous,
        compare("Price 10", "Price 10", FilterConfig()),
    )
    state = store.list_monitors()[0]
    assert state["consecutive_failures"] == 0
    assert state["last_error"] is None
    assert state["check_count"] == 4


def test_removing_and_restoring_monitor_archives_state_preserving_history(store, monitored):
    snapshot_id, _ = save_snapshot(store, monitored, "Price 10")

    store.sync_monitors([])

    assert store.list_monitors() == []
    assert store.snapshot(snapshot_id) is not None
    assert len(store.history(monitored.id)) == 1
    with store.sessions() as session:
        archived = session.get(MonitorState, monitored.id)
        assert archived.active is False
        assert archived.enabled is False
        assert archived.check_count == 1

    updated = monitored.model_copy(update={"name": "New name", "interval": "1h"})
    store.sync_monitors([updated])
    state = store.list_monitors()[0]
    assert state["active"] is True
    assert state["enabled"] is True
    assert state["name"] == "New name"
    assert state["interval"] == "1h"
    assert state["check_count"] == 1
    assert store.latest(monitored.id)["id"] == snapshot_id


def test_reopening_sqlite_preserves_snapshots_events_and_delivery_results(tmp_path, monitored):
    url = f"sqlite:///{tmp_path / 'persistent.db'}"
    first = Store(url)
    try:
        first.sync_monitors([monitored])
        baseline_id, _ = save_snapshot(first, monitored, "abcd")
        change = compare("abcd", "wxyz", FilterConfig(threshold_percent=0))
        changed_id, event_id = save_snapshot(
            first, monitored, "wxyz", "changed", first.latest(monitored.id), change
        )
        first.record_deliveries(event_id, [{"channel": "mail", "success": True, "error": None}])
    finally:
        first.close()

    reopened = Store(url)
    try:
        reopened.sync_monitors([monitored])
        assert reopened.latest(monitored.id)["id"] == changed_id
        assert reopened.snapshot(baseline_id)["text"] == "abcd"
        assert reopened.events(monitored.id)[0]["deliveries"][0]["success"] is True
        assert reopened.list_monitors()[0]["check_count"] == 2
        assert reopened.list_monitors()[0]["status"] == "changed"
    finally:
        reopened.close()


def test_history_order_and_limit_return_latest_snapshots(store, monitored):
    ids = [save_snapshot(store, monitored, str(index))[0] for index in range(4)]
    assert [item["id"] for item in store.history(monitored.id, limit=2)] == ids[::-1][:2]
    assert len(store.history(monitored.id, limit=0)) == 1
    assert store.history("missing") == []
    assert store.events("missing") == []
    assert store.latest("missing") is None


def test_unknown_monitor_cannot_leave_orphan_snapshot(store, monitored):
    unregistered = monitored.model_copy(update={"id": "unregistered"})
    with pytest.raises(IntegrityError):
        save_snapshot(store, unregistered, "Orphan")
    with store.sessions() as session:
        assert list(session.scalars(select(Snapshot))) == []
