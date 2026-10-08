"""SQLAlchemy persistence, with SQLite defaults and PostgreSQL support."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import MonitorConfig


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MonitorState(Base):
    __tablename__ = "monitors"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    engine: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(32))
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    check_count: Mapped[int] = mapped_column(Integer, default=0)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("monitors.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    content_hash: Mapped[str] = mapped_column(String(64))
    config_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    clean_html: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    image_hash: Mapped[str | None] = mapped_column(String(64))


class ChangeEvent(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("monitors.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    previous_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshots.id"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"))
    status: Mapped[str] = mapped_column(String(32))
    difference_percent: Mapped[float] = mapped_column(Float)
    visual_difference_percent: Mapped[float] = mapped_column(Float, default=0)
    changed_chars: Mapped[int] = mapped_column(Integer)
    diff: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    deliveries: Mapped[list] = mapped_column(JSON, default=list)


def _as_dict(record: Base) -> dict[str, Any]:
    result = {}
    for column in record.__table__.columns:
        value = getattr(record, column.name)
        if isinstance(value, datetime):
            value = value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
        result[column.name] = value
    return result


class Store:
    def __init__(self, database_url: str):
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        url = make_url(database_url)
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.get_backend_name() == "sqlite":
            if url.database and url.database != ":memory:":
                Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if not url.database or url.database == ":memory:":
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **kwargs)
        if url.get_backend_name() == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def sync_monitors(self, monitors: list[MonitorConfig]) -> None:
        ids = {monitor.id for monitor in monitors}
        with self.sessions.begin() as session:
            for state in session.scalars(select(MonitorState)):
                if state.id not in ids:
                    state.active = False
                    state.enabled = False
            for monitor in monitors:
                state = session.get(MonitorState, monitor.id)
                if state is None:
                    state = MonitorState(id=monitor.id)
                    session.add(state)
                state.name = monitor.name or monitor.id
                state.url = monitor.url
                state.enabled = monitor.enabled
                state.active = True
                state.engine = monitor.engine
                state.interval = monitor.interval

    def list_monitors(self) -> list[dict]:
        with self.sessions() as session:
            return [
                _as_dict(row)
                for row in session.scalars(
                    select(MonitorState)
                    .where(MonitorState.active.is_(True))
                    .order_by(MonitorState.id)
                )
            ]

    def latest(self, monitor_id: str) -> dict | None:
        with self.sessions() as session:
            row = session.scalar(
                select(Snapshot)
                .where(Snapshot.monitor_id == monitor_id)
                .order_by(Snapshot.id.desc())
                .limit(1)
            )
            return _as_dict(row) if row else None

    def record_success(
        self,
        monitor_id: str,
        extracted,
        captured,
        status: str,
        previous: dict | None,
        change=None,
        fingerprint: str = "",
    ) -> tuple[int, int | None]:
        with self.sessions.begin() as session:
            snapshot = Snapshot(
                monitor_id=monitor_id,
                content_hash=extracted.content_hash,
                config_fingerprint=fingerprint,
                clean_html=extracted.clean_html,
                text=extracted.text,
                screenshot_path=captured.screenshot_path,
                image_hash=captured.image_hash,
            )
            session.add(snapshot)
            session.flush()
            event_id = None
            if change and change.changed:
                change_event = ChangeEvent(
                    monitor_id=monitor_id,
                    previous_snapshot_id=previous["id"] if previous else None,
                    snapshot_id=snapshot.id,
                    status=status,
                    difference_percent=change.difference_percent,
                    visual_difference_percent=change.visual_difference_percent,
                    changed_chars=change.changed_chars,
                    diff=change.diff,
                    reason=change.reason,
                    deliveries=[],
                )
                session.add(change_event)
                session.flush()
                event_id = change_event.id
            state = session.get(MonitorState, monitor_id)
            if state is None:
                raise ValueError("Monitor is not registered")
            state.last_checked = now()
            state.last_success = state.last_checked
            state.last_error = None
            state.status = status
            state.consecutive_failures = 0
            state.check_count += 1
            return snapshot.id, event_id

    def record_failure(self, monitor_id: str, error: str) -> None:
        with self.sessions.begin() as session:
            state = session.get(MonitorState, monitor_id)
            if state:
                state.last_checked = now()
                state.last_error = error
                state.status = "error"
                state.consecutive_failures += 1
                state.check_count += 1

    def record_deliveries(self, event_id: int, deliveries: list[dict]) -> None:
        with self.sessions.begin() as session:
            row = session.get(ChangeEvent, event_id)
            if row:
                row.deliveries = deliveries

    def history(self, monitor_id: str, limit: int = 50) -> list[dict]:
        with self.sessions() as session:
            return [
                _as_dict(row)
                for row in session.scalars(
                    select(Snapshot)
                    .where(Snapshot.monitor_id == monitor_id)
                    .order_by(Snapshot.id.desc())
                    .limit(max(1, min(limit, 1000)))
                )
            ]

    def events(self, monitor_id: str, limit: int = 50) -> list[dict]:
        with self.sessions() as session:
            return [
                _as_dict(row)
                for row in session.scalars(
                    select(ChangeEvent)
                    .where(ChangeEvent.monitor_id == monitor_id)
                    .order_by(ChangeEvent.id.desc())
                    .limit(max(1, min(limit, 1000)))
                )
            ]

    def snapshot(self, snapshot_id: int) -> dict | None:
        with self.sessions() as session:
            row = session.get(Snapshot, snapshot_id)
            return _as_dict(row) if row else None

    def close(self) -> None:
        self.engine.dispose()
