"""Validated YAML configuration; credentials remain environment references on save."""

from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import soupsieve
import yaml
from lxml import etree
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PrivateAttr,
    TypeAdapter,
    field_validator,
    model_validator,
)

ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
INTERVAL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)\s*$", re.I)
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
]


def interval_seconds(value: str) -> float:
    match = INTERVAL.fullmatch(value)
    if not match:
        raise ValueError("Interval must use a positive duration such as 30s, 5m, 1h or 1d")
    seconds = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2].lower()]
    if seconds < 1 or seconds > 365 * 86400:
        raise ValueError("Interval must be between 1 second and 365 days")
    return seconds


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FilterConfig(StrictModel):
    threshold_percent: float = Field(default=1.0, ge=0, le=100)
    min_changed_chars: int = Field(default=1, ge=0)
    keywords: list[str] = Field(default_factory=list)
    ignore_selectors: list[str] = Field(default_factory=list)
    ignore_patterns: list[str] = Field(default_factory=list)
    ignore_case: bool = False

    @field_validator("keywords")
    @classmethod
    def nonempty_keywords(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Keywords must not be empty")
        return [value.strip() for value in values]

    @field_validator("ignore_patterns")
    @classmethod
    def valid_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError("Invalid ignore_patterns regular expression") from exc
        return values

    @field_validator("ignore_selectors")
    @classmethod
    def valid_selectors(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                soupsieve.compile(value)
            except Exception as exc:
                raise ValueError("Invalid CSS ignore selector") from exc
        return values


class MonitorConfig(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    name: str = ""
    url: str
    selector: str | None = None
    selector_type: Literal["css", "xpath", "full"] = "full"
    engine: Literal["http", "playwright"] = "http"
    interval: str = "5m"
    enabled: bool = True
    headless: bool = True
    visual: bool = False
    image_hash: bool = False
    timeout: float = Field(default=30, gt=0, le=600)
    retries: int = Field(default=3, ge=0, le=10)
    retry_backoff: float = Field(default=1, ge=0, le=300)
    user_agents: list[str] = Field(default_factory=lambda: DEFAULT_USER_AGENTS.copy(), min_length=1)
    proxy: str | None = None
    filters: FilterConfig = Field(default_factory=FilterConfig)
    channels: list[str] = Field(default_factory=list)

    @field_validator("proxy")
    @classmethod
    def valid_proxy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            port = parsed.port
            valid = (
                parsed.scheme in ("http", "https", "socks5")
                and parsed.hostname
                and not parsed.query
                and not parsed.fragment
                and parsed.path in ("", "/")
            )
            if not valid or (port is not None and port < 1):
                raise ValueError
        except ValueError as exc:
            raise ValueError("Proxy must be a valid HTTP, HTTPS or SOCKS5 URL") from exc
        return value

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = TypeAdapter(HttpUrl).validate_python(value)
        if parsed.username or parsed.password:
            raise ValueError(
                "Use proxy/environment configuration instead of credentials in target URLs"
            )
        return str(parsed)

    @field_validator("interval")
    @classmethod
    def valid_interval(cls, value: str) -> str:
        interval_seconds(value)
        return value.strip().lower()

    @model_validator(mode="after")
    def valid_selector(self) -> MonitorConfig:
        if self.selector_type != "full" and not self.selector:
            raise ValueError("CSS and XPath modes require a nonempty selector")
        if self.selector:
            try:
                if self.selector_type == "css":
                    soupsieve.compile(self.selector)
                elif self.selector_type == "xpath":
                    etree.XPath(self.selector)
            except Exception as exc:
                raise ValueError("Invalid CSS/XPath selector") from exc
        return self


class NotificationConfig(StrictModel):
    kind: Literal["email", "telegram", "discord", "slack", "desktop"]
    host: str = "localhost"
    port: int = Field(default=587, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    from_address: str | None = None
    to_addresses: list[str] = Field(default_factory=list)
    starttls: bool = True
    ssl: bool = False
    bot_token: str | None = None
    chat_id: str | None = None
    webhook_url: str | None = None

    @model_validator(mode="after")
    def required_values(self) -> NotificationConfig:
        if self.kind == "email" and (not self.from_address or not self.to_addresses):
            raise ValueError("Email requires from_address and to_addresses")
        if self.kind == "telegram" and (not self.bot_token or not self.chat_id):
            raise ValueError("Telegram requires bot_token and chat_id")
        if self.kind in ("slack", "discord"):
            if not self.webhook_url:
                raise ValueError("Webhook channel requires webhook_url")
            parsed = TypeAdapter(HttpUrl).validate_python(self.webhook_url)
            if parsed.scheme != "https":
                raise ValueError("Webhook URLs must use HTTPS")
        return self


class AppConfig(StrictModel):
    database_url: str = "sqlite:///data/sentinel.db"
    snapshot_dir: Path = Path("data/screenshots")
    concurrency: int = Field(default=4, ge=1, le=64)
    monitors: list[MonitorConfig] = Field(default_factory=list)
    notifications: dict[str, NotificationConfig] = Field(default_factory=dict)
    _raw_config: dict[str, Any] = PrivateAttr(default_factory=dict)
    _resolved_config: dict[str, Any] = PrivateAttr(default_factory=dict)

    @field_validator("database_url")
    @classmethod
    def valid_database(cls, value: str) -> str:
        if not value.startswith(
            ("sqlite://", "sqlite+pysqlite://", "postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("database_url must use SQLite or PostgreSQL (psycopg)")
        return value

    @model_validator(mode="after")
    def unique_monitors_and_channels(self) -> AppConfig:
        ids = [monitor.id for monitor in self.monitors]
        if len(ids) != len(set(ids)):
            raise ValueError("Monitor ids must be unique")
        for monitor in self.monitors:
            if set(monitor.channels) - self.notifications.keys():
                raise ValueError(
                    f"Monitor {monitor.id} refers to an undefined notification channel"
                )
        return self


def _expand(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match) -> str:
            name = match[1]
            if name not in os.environ:
                raise ValueError(f"Required environment variable {name} is not set")
            return os.environ[name]

        return ENV_REFERENCE.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    if not isinstance(raw, dict):
        raise ValueError("YAML configuration must be a mapping")
    config = AppConfig.model_validate(_expand(raw))
    config._raw_config = copy.deepcopy(raw)
    config._resolved_config = config.model_dump(mode="json")
    return config


def _preserve_references(raw: Any, current: Any, resolved: Any) -> Any:
    if isinstance(raw, str) and ENV_REFERENCE.search(raw):
        # Compare validated values, including normalized URLs and typed ports.
        if current == resolved:
            return raw
    if isinstance(raw, dict) and isinstance(current, dict):
        return {
            key: _preserve_references(
                raw.get(key), item, resolved.get(key) if isinstance(resolved, dict) else None
            )
            for key, item in current.items()
        }
    if isinstance(raw, list) and isinstance(current, list):
        # Match monitors by id so deleting/reordering does not shift secret references.
        resolved_list = resolved if isinstance(resolved, list) else []
        # YAML ids can themselves be ${ENV} references. Pair raw entries with
        # their validated baseline before indexing so other secret references
        # remain attached to the correct monitor after reordering/deletion.
        indexed = {
            validated["id"]: original
            for original, validated in zip(raw, resolved_list)
            if isinstance(original, dict) and isinstance(validated, dict) and "id" in validated
        }
        resolved_indexed = {
            item["id"]: item for item in resolved_list if isinstance(item, dict) and "id" in item
        }
        return [
            _preserve_references(
                indexed.get(item.get("id"))
                if isinstance(item, dict) and "id" in item
                else (raw[index] if index < len(raw) else None),
                item,
                resolved_indexed.get(item.get("id"))
                if isinstance(item, dict) and "id" in item
                else (resolved_list[index] if index < len(resolved_list) else None),
            )
            for index, item in enumerate(current)
        ]
    return current


def save_config(config: AppConfig, path: str | Path) -> None:
    # model_copy(update=...) skips validation, so revalidate before persisting.
    current = config.model_dump(mode="json")
    AppConfig.model_validate(current)
    data = _preserve_references(config._raw_config, current, config._resolved_config)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".sentinel-", delete=False
        ) as stream:
            temporary = stream.name
            yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    config._raw_config = copy.deepcopy(data)
    config._resolved_config = copy.deepcopy(current)
