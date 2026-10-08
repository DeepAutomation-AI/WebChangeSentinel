"""Independent, asynchronous notification delivery with sanitized failures.

Notification providers receive only a compact summary and a link to the page.
Failures are deliberately sanitized: HTTP URLs, bot tokens and SMTP passwords
must never reach application logs or the dashboard through provider exceptions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import smtplib
import ssl
import subprocess
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING

import httpx
from telegram import Bot

if TYPE_CHECKING:
    from .config import NotificationConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Alert:
    monitor_id: str
    monitor_name: str
    url: str
    difference_percent: float
    diff: str
    visual_difference_percent: float = 0.0


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    channel: str
    success: bool
    error: str | None = None


class _DeliveryError(Exception):
    """An error whose message is constant and safe to display publicly."""


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _message(alert: Alert, limit: int) -> str:
    name = _shorten(" ".join(alert.monitor_name.split()), 180)
    url = _shorten(alert.url, min(1000, limit // 2))
    heading = (
        f"WebChangeSentinel — {name}\n"
        f"{url}\n"
        f"Cambio textual: {alert.difference_percent:.2f}%"
    )
    if alert.visual_difference_percent:
        heading += f" | Cambio visual: {alert.visual_difference_percent:.2f}%"
    heading += "\n\n"
    return _shorten(heading + alert.diff, limit)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, _DeliveryError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "El proveedor agotó el tiempo de espera."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"El proveedor rechazó la notificación (HTTP {exc.response.status_code})."
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "La autenticación SMTP falló."
    if isinstance(exc, smtplib.SMTPException):
        return "El servidor SMTP rechazó la notificación."
    # Do not interpolate str(exc): it frequently contains a webhook URL/token.
    return "No se pudo enviar la notificación; revisa la configuración del canal."


class NotificationDispatcher:
    """Deliver channels concurrently, preserving result order and isolation."""

    def __init__(self, configs: dict[str, NotificationConfig]) -> None:
        self.configs = configs

    async def send(self, alert: Alert, channels: list[str]) -> list[DeliveryResult]:
        # A repeated channel name should not deliver the same alert twice.
        unique_channels = list(dict.fromkeys(channels))
        return list(await asyncio.gather(*(self._send_one(alert, name) for name in unique_channels)))

    async def _send_one(self, alert: Alert, channel: str) -> DeliveryResult:
        config = self.configs.get(channel)
        if config is None:
            return DeliveryResult(channel, False, "Canal de notificación no configurado.")
        try:
            if config.kind == "email":
                await asyncio.to_thread(self._email, config, alert)
            elif config.kind == "telegram":
                await self._telegram(config, alert)
            elif config.kind in {"discord", "slack"}:
                await self._webhook(config, alert)
            elif config.kind == "desktop":
                await asyncio.to_thread(self._desktop, alert)
            else:
                raise _DeliveryError("Tipo de canal no compatible.")
        except Exception as exc:
            logger.warning(
                "Notification delivery failed (kind=%s, error_type=%s)",
                config.kind,
                type(exc).__name__,
            )
            return DeliveryResult(channel, False, _safe_error(exc))
        return DeliveryResult(channel, True)

    @staticmethod
    def _email(config: NotificationConfig, alert: Alert) -> None:
        if not config.from_address or not config.to_addresses:
            raise _DeliveryError("Email requiere remitente y destinatarios.")
        if config.username and not config.password:
            raise _DeliveryError("La autenticación SMTP requiere una contraseña.")
        email = EmailMessage()
        name = _shorten(" ".join(alert.monitor_name.split()), 180)
        email["Subject"] = f"[WebChangeSentinel] Cambios en {name}"
        email["From"] = config.from_address
        email["To"] = ", ".join(config.to_addresses)
        email.set_content(_message(alert, 12000))
        context = ssl.create_default_context()
        if config.ssl:
            connection = smtplib.SMTP_SSL(config.host, config.port, timeout=15, context=context)
        else:
            connection = smtplib.SMTP(config.host, config.port, timeout=15)
        with connection as smtp:
            if config.starttls and not config.ssl:
                smtp.starttls(context=context)
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(email, from_addr=config.from_address, to_addrs=config.to_addresses)

    @staticmethod
    async def _telegram(config: NotificationConfig, alert: Alert) -> None:
        if not config.bot_token or not config.chat_id:
            raise _DeliveryError("Telegram requiere bot_token y chat_id.")
        async with Bot(token=config.bot_token) as bot:
            await bot.send_message(
                chat_id=config.chat_id,
                text=_message(alert, 4000),
                disable_web_page_preview=True,
            )

    @staticmethod
    async def _webhook(config: NotificationConfig, alert: Alert) -> None:
        if not config.webhook_url:
            raise _DeliveryError("El canal requiere webhook_url.")
        if config.kind == "discord":
            payload = {"content": _message(alert, 2000), "allowed_mentions": {"parse": []}}
        else:
            payload = {
                "text": _message(alert, 3000),
                "mrkdwn": False,
                "unfurl_links": False,
                "unfurl_media": False,
            }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(config.webhook_url, json=payload)
            response.raise_for_status()
            # Slack may return HTTP 200 with a plaintext failure response.
            if config.kind == "slack" and response.text.strip() != "ok":
                raise _DeliveryError("Slack no confirmó la entrega de la notificación.")

    @staticmethod
    def _desktop(alert: Alert) -> None:
        if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            raise _DeliveryError(
                "Las notificaciones desktop requieren una sesión gráfica (DISPLAY o WAYLAND_DISPLAY)."
            )
        try:
            from plyer import notification
        except ImportError:
            notification = None
        title = _shorten(f"WebChangeSentinel: {' '.join(alert.monitor_name.split())}", 200)
        message = _message(alert, 1200)
        if notification is not None:
            notification.notify(title=title, message=message, app_name="WebChangeSentinel", timeout=10)
            return
        executable = shutil.which("notify-send")
        if not executable:
            raise _DeliveryError("Instala plyer o notify-send para notificaciones desktop.")
        subprocess.run(
            [executable, "--app-name=WebChangeSentinel", "--expire-time=10000", "--", title, message],
            check=True,
            timeout=15,
            capture_output=True,
        )
