"""All notification providers are mocked; no real alerts are transmitted."""

from __future__ import annotations

import smtplib
import ssl
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest

from webchangesentinel import notifications
from webchangesentinel.config import NotificationConfig
from webchangesentinel.notifications import Alert, NotificationDispatcher


@pytest.fixture
def alert() -> Alert:
    return Alert(
        monitor_id="price",
        monitor_name="Precio de ejemplo",
        url="https://example.com/product",
        difference_percent=12.5,
        diff="--- antes\n+++ después\n-10 euros\n+11 euros",
        visual_difference_percent=3.0,
    )


def email_config(**overrides) -> NotificationConfig:
    values = {
        "kind": "email",
        "host": "smtp.example.com",
        "port": 587,
        "from_address": "sentinel@example.com",
        "to_addresses": ["owner@example.com"],
        "username": "sentinel",
        "password": "smtp-secret",
    }
    return NotificationConfig(**(values | overrides))


@pytest.mark.asyncio
async def test_email_uses_verified_starttls_and_sends_message(monkeypatch, alert):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    factory = Mock(return_value=smtp)
    monkeypatch.setattr(notifications.smtplib, "SMTP", factory)

    result = await NotificationDispatcher({"mail": email_config()}).send(alert, ["mail"])

    assert result[0].success
    factory.assert_called_once_with("smtp.example.com", 587, timeout=15)
    context = smtp.starttls.call_args.kwargs["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    smtp.login.assert_called_once_with("sentinel", "smtp-secret")
    message = smtp.send_message.call_args.args[0]
    assert message["From"] == "sentinel@example.com"
    assert message["To"] == "owner@example.com"
    assert alert.url in message.get_content()
    assert "12.50%" in message.get_content()
    smtp.__exit__.assert_called_once()


@pytest.mark.asyncio
async def test_email_ssl_does_not_attempt_starttls(monkeypatch, alert):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    factory = Mock(return_value=smtp)
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", factory)

    result = await NotificationDispatcher(
        {"mail": email_config(ssl=True, port=465)}
    ).send(alert, ["mail"])

    assert result[0].success
    assert factory.call_args.kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    smtp.starttls.assert_not_called()


@pytest.mark.asyncio
async def test_smtp_authentication_errors_do_not_expose_password(monkeypatch, alert, caplog):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"smtp-secret")
    monkeypatch.setattr(notifications.smtplib, "SMTP", Mock(return_value=smtp))

    result = await NotificationDispatcher({"mail": email_config()}).send(alert, ["mail"])

    assert not result[0].success
    assert "autenticación" in result[0].error
    assert "smtp-secret" not in result[0].error + caplog.text


@pytest.mark.asyncio
async def test_telegram_is_plain_bounded_text_and_closes_bot(monkeypatch, alert):
    bot = AsyncMock()
    bot.__aenter__.return_value = bot
    factory = Mock(return_value=bot)
    monkeypatch.setattr(notifications, "Bot", factory)
    config = NotificationConfig(kind="telegram", bot_token="bot-secret", chat_id="123")
    long_alert = Alert(alert.monitor_id, alert.monitor_name, alert.url, 25, "x" * 8000)

    result = await NotificationDispatcher({"tg": config}).send(long_alert, ["tg"])

    assert result[0].success
    factory.assert_called_once_with(token="bot-secret")
    args = bot.send_message.call_args.kwargs
    assert args["chat_id"] == "123"
    assert len(args["text"]) <= 4000
    assert alert.url in args["text"]
    assert args["disable_web_page_preview"] is True
    assert "parse_mode" not in args
    bot.__aexit__.assert_awaited_once()


def mocked_webhook_client(monkeypatch, response):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr(notifications.httpx, "AsyncClient", factory)
    return client, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,field,limit", [("discord", "content", 2000), ("slack", "text", 3000)])
async def test_webhooks_bound_text_and_include_link(monkeypatch, alert, kind, field, limit):
    response = Mock(text="ok")
    client, factory = mocked_webhook_client(monkeypatch, response)
    webhook_url = "https://example.com/webhook-secret"
    config = NotificationConfig(kind=kind, webhook_url=webhook_url)
    long_alert = Alert(alert.monitor_id, alert.monitor_name, alert.url, 25, "x" * 9000)

    result = await NotificationDispatcher({"hook": config}).send(long_alert, ["hook"])

    assert result[0].success
    factory.assert_called_once_with(timeout=15)
    assert client.post.call_args.args[0] == webhook_url
    payload = client.post.call_args.kwargs["json"]
    assert len(payload[field]) <= limit
    assert alert.url in payload[field]
    if kind == "discord":
        assert payload["allowed_mentions"] == {"parse": []}
    else:
        assert payload["mrkdwn"] is False
        assert payload["unfurl_links"] is False
    response.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_slack_non_ok_response_is_a_failed_delivery(monkeypatch, alert):
    mocked_webhook_client(monkeypatch, Mock(text="invalid_payload"))
    config = NotificationConfig(kind="slack", webhook_url="https://example.com/slack")

    result = await NotificationDispatcher({"slack": config}).send(alert, ["slack"])

    assert not result[0].success
    assert "Slack" in result[0].error


@pytest.mark.asyncio
async def test_failed_webhook_is_sanitized_and_other_channels_continue(monkeypatch, alert, caplog):
    url = "https://example.com/secret-token"
    request = httpx.Request("POST", url)
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError(f"Forbidden request to {url}", request=request, response=response)
    failing_response = Mock()
    failing_response.raise_for_status.side_effect = error
    mocked_webhook_client(monkeypatch, failing_response)
    desktop = Mock()
    monkeypatch.setattr(NotificationDispatcher, "_desktop", desktop)
    dispatcher = NotificationDispatcher({
        "bad": NotificationConfig(kind="discord", webhook_url=url),
        "good": NotificationConfig(kind="desktop"),
    })

    results = await dispatcher.send(alert, ["bad", "good", "unknown"])

    assert [result.channel for result in results] == ["bad", "good", "unknown"]
    assert [result.success for result in results] == [False, True, False]
    assert "403" in results[0].error
    assert "secret-token" not in (results[0].error + caplog.text)
    desktop.assert_called_once_with(alert)


@pytest.mark.asyncio
async def test_telegram_provider_exception_is_sanitized(monkeypatch, alert, caplog):
    factory = Mock(side_effect=ValueError("Invalid token: super-secret-token"))
    monkeypatch.setattr(notifications, "Bot", factory)
    config = NotificationConfig(kind="telegram", bot_token="super-secret-token", chat_id="123")

    result = await NotificationDispatcher({"tg": config}).send(alert, ["tg"])

    assert not result[0].success
    assert "super-secret-token" not in result[0].error + caplog.text


@pytest.mark.asyncio
async def test_desktop_without_display_reports_clear_failure(monkeypatch, alert):
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    result = await NotificationDispatcher({"desktop": NotificationConfig(kind="desktop")}).send(
        alert, ["desktop"]
    )

    assert not result[0].success
    assert "sesión gráfica" in result[0].error


@pytest.mark.asyncio
async def test_desktop_plyer_delivery_is_mocked(monkeypatch, alert):
    monkeypatch.setenv("DISPLAY", ":99")
    notification = Mock()
    monkeypatch.setitem(sys.modules, "plyer", SimpleNamespace(notification=notification))

    result = await NotificationDispatcher({"desktop": NotificationConfig(kind="desktop")}).send(
        alert, ["desktop", "desktop"]
    )

    assert len(result) == 1
    assert result[0].success
    notification.notify.assert_called_once()
    assert alert.url in notification.notify.call_args.kwargs["message"]


@pytest.mark.asyncio
async def test_desktop_notify_send_fallback_avoids_shell(monkeypatch, alert):
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setitem(sys.modules, "plyer", None)
    monkeypatch.setattr(notifications.shutil, "which", Mock(return_value="/usr/bin/notify-send"))
    run = Mock()
    monkeypatch.setattr(notifications.subprocess, "run", run)

    result = await NotificationDispatcher({"desktop": NotificationConfig(kind="desktop")}).send(
        alert, ["desktop"]
    )

    assert result[0].success
    assert run.call_args.args[0][0] == "/usr/bin/notify-send"
    assert "--" in run.call_args.args[0]
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["timeout"] == 15
    assert not run.call_args.kwargs.get("shell", False)


@pytest.mark.asyncio
async def test_empty_channels_do_not_send_anything(alert):
    assert await NotificationDispatcher({}).send(alert, []) == []
