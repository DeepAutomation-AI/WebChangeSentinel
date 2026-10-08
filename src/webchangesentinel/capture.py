"""Fetch a page with bounded retries and optional browser screenshots."""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import httpx
import imagehash
from PIL import Image
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from .config import MonitorConfig

logger = logging.getLogger(__name__)
_user_agent_sequences: dict[str, itertools.count] = {}
DEFAULT_USER_AGENT = "WebChangeSentinel/1.0 (+web-change-monitor)"


@dataclass(frozen=True)
class FetchedPage:
    html: str
    screenshot_path: str | None = None
    image_hash: str | None = None


class CaptureError(RuntimeError):
    """A capture exhausted its retries; messages never expose credentials."""


def _user_agent(monitor: MonitorConfig) -> str:
    agents = monitor.user_agents or [DEFAULT_USER_AGENT]
    sequence = _user_agent_sequences.setdefault(monitor.id, itertools.count())
    return agents[next(sequence) % len(agents)]


async def _fetch_http(monitor: MonitorConfig, user_agent: str) -> FetchedPage:
    kwargs = {
        "timeout": monitor.timeout,
        "follow_redirects": True,
        "headers": {"User-Agent": user_agent},
    }
    if monitor.proxy:
        kwargs["proxy"] = monitor.proxy
    async with httpx.AsyncClient(**kwargs) as client:
        response = await client.get(str(monitor.url))
        response.raise_for_status()
        return FetchedPage(html=response.text)


def _browser_proxy(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    host = parsed.hostname
    if not parsed.scheme or not host:
        raise ValueError("Proxy must include a scheme and host")
    if ":" in host:
        host = f"[{host}]"
    server = f"{parsed.scheme}://{host}"
    if parsed.port is not None:
        server += f":{parsed.port}"
    options = {"server": server}
    if parsed.username is not None:
        options["username"] = unquote(parsed.username)
    if parsed.password is not None:
        options["password"] = unquote(parsed.password)
    return options


async def _fetch_browser(
    monitor: MonitorConfig, screenshot_dir: Path, user_agent: str
) -> FetchedPage:
    screenshot_path: Path | None = None
    browser = None
    try:
        async with async_playwright() as playwright:
            launch_options = {
                "headless": monitor.headless,
                "timeout": int(monitor.timeout * 1000),
            }
            if monitor.proxy:
                launch_options["proxy"] = _browser_proxy(monitor.proxy)
            browser = await playwright.chromium.launch(**launch_options)
            try:
                context = await browser.new_context(
                    user_agent=user_agent,
                    viewport={"width": 1280, "height": 800},
                    device_scale_factor=1,
                )
                page = await context.new_page()
                response = await page.goto(
                    str(monitor.url),
                    wait_until="domcontentloaded",
                    timeout=int(monitor.timeout * 1000),
                )
                if response is not None and response.status >= 400:
                    raise CaptureError(f"Browser received HTTP status {response.status}")
                if monitor.selector_type == "css" and monitor.selector:
                    await page.wait_for_selector(
                        monitor.selector, state="attached", timeout=int(monitor.timeout * 1000)
                    )
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=min(int(monitor.timeout * 1000), 2000)
                    )
                except PlaywrightTimeoutError:
                    # Tracking and long polling may never become idle. DOM and
                    # the configured CSS target are already available.
                    logger.debug("Network remained active; capturing loaded DOM")
                html = await page.content()
                perceptual_hash = None
                if monitor.visual or monitor.image_hash:
                    screenshot_dir.mkdir(parents=True, exist_ok=True)
                    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", monitor.id)[:80]
                    screenshot_path = screenshot_dir / f"{safe_id}-{uuid.uuid4().hex}.png"
                    await page.screenshot(
                        path=str(screenshot_path), full_page=True, animations="disabled"
                    )
                    with Image.open(screenshot_path) as screenshot:
                        perceptual_hash = str(imagehash.phash(screenshot))
                return FetchedPage(
                    html=html,
                    screenshot_path=str(screenshot_path) if screenshot_path else None,
                    image_hash=perceptual_hash,
                )
            finally:
                await browser.close()
    except BaseException:
        # Failed attempts must not leave orphaned screenshots, including cancellation.
        if screenshot_path is not None:
            screenshot_path.unlink(missing_ok=True)
        raise


async def fetch(monitor: MonitorConfig, screenshot_dir: Path) -> FetchedPage:
    """Capture a URL, rotating user agents and retrying with exponential backoff.

    ``retries`` counts additional attempts. Visual capture requires Chromium even
    when ``engine`` is HTTP. Screenshots use unique names and a fixed viewport;
    ``visual`` and ``image_hash`` both calculate a perceptual screenshot hash.
    TLS and browser certificate verification retain their secure defaults.
    """
    attempts = monitor.retries + 1
    for attempt in range(attempts):
        try:
            agent = _user_agent(monitor)
            if monitor.engine == "playwright" or monitor.visual or monitor.image_hash:
                return await _fetch_browser(monitor, Path(screenshot_dir), agent)
            return await _fetch_http(monitor, agent)
        except Exception as exc:
            # URLs, proxies and third-party exception messages can contain secrets.
            # Log only the attempt and exception class, never their raw values.
            if attempt + 1 == attempts:
                raise CaptureError(
                    f"Capture failed after {attempts} attempt(s): {type(exc).__name__}"
                ) from None
            logger.warning(
                "Capture attempt %d/%d failed (%s); retrying",
                attempt + 1,
                attempts,
                type(exc).__name__,
            )
            await asyncio.sleep(monitor.retry_backoff * (2**attempt))
    raise AssertionError("Unreachable retry state")
