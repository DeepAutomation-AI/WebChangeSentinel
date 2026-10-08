"""Capture tests use deterministic network/browser doubles, never live sites."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from webchangesentinel import capture


@pytest.fixture(autouse=True)
def reset_user_agent_sequences(monkeypatch):
    monkeypatch.setattr(capture, "_user_agent_sequences", {})


def monitor(**updates):
    values = dict(
        id="example",
        url="https://example.test",
        engine="http",
        selector=None,
        selector_type="full",
        headless=True,
        visual=False,
        image_hash=False,
        timeout=5.0,
        retries=2,
        retry_backoff=0.25,
        user_agents=["Agent-A", "Agent-B"],
        proxy=None,
    )
    values.update(updates)
    return SimpleNamespace(**values)


def mock_http(monkeypatch, responses):
    client = SimpleNamespace(get=AsyncMock(side_effect=responses))
    manager = SimpleNamespace(
        __aenter__=AsyncMock(return_value=client), __aexit__=AsyncMock(return_value=False)
    )

    class AsyncManager:
        async def __aenter__(self):
            return await manager.__aenter__()

        async def __aexit__(self, *args):
            return await manager.__aexit__(*args)

    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return AsyncManager()

    monkeypatch.setattr(capture.httpx, "AsyncClient", factory)
    return client, calls


def response(status=200, text="<h1>Success</h1>"):
    return httpx.Response(status, text=text, request=httpx.Request("GET", "https://example.test"))


@pytest.mark.asyncio
async def test_http_capture_preserves_tls_and_supports_proxy(monkeypatch, tmp_path):
    client, calls = mock_http(monkeypatch, [response()])
    result = await capture.fetch(monitor(proxy="http://proxy.test:8080"), tmp_path / "shots")
    assert result.html == "<h1>Success</h1>"
    assert result.screenshot_path is None and result.image_hash is None
    assert calls[0]["proxy"] == "http://proxy.test:8080"
    assert calls[0]["headers"]["User-Agent"] == "Agent-A"
    assert calls[0]["follow_redirects"] is True
    assert "verify" not in calls[0]
    assert not (tmp_path / "shots").exists()
    client.get.assert_awaited_once_with("https://example.test")


@pytest.mark.asyncio
async def test_retry_backoff_rotates_user_agents(monkeypatch, tmp_path):
    _, calls = mock_http(
        monkeypatch,
        [httpx.ConnectError("temporarily unavailable"), response(503), response()],
    )
    sleep = AsyncMock()
    monkeypatch.setattr(capture.asyncio, "sleep", sleep)
    result = await capture.fetch(monitor(), tmp_path)
    assert result.html == "<h1>Success</h1>"
    assert [call["headers"]["User-Agent"] for call in calls] == ["Agent-A", "Agent-B", "Agent-A"]
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]


@pytest.mark.asyncio
async def test_each_monitor_rotates_agents_independently_across_concurrent_cycles(monkeypatch, tmp_path):
    client, calls = mock_http(monkeypatch, [response() for _ in range(6)])
    monitors = [
        monitor(id="first", url="https://first.test"),
        monitor(id="second", url="https://second.test"),
    ]
    for _ in range(3):
        await asyncio.gather(*(capture.fetch(current, tmp_path) for current in monitors))

    agents_by_url = {current.url: [] for current in monitors}
    for request, options in zip(client.get.await_args_list, calls, strict=True):
        agents_by_url[request.args[0]].append(options["headers"]["User-Agent"])
    assert agents_by_url == {
        "https://first.test": ["Agent-A", "Agent-B", "Agent-A"],
        "https://second.test": ["Agent-A", "Agent-B", "Agent-A"],
    }


@pytest.mark.asyncio
async def test_exhaustion_omits_sensitive_error_details(monkeypatch, tmp_path, caplog):
    secret = "proxy-password-do-not-print"
    _, calls = mock_http(monkeypatch, [httpx.ConnectError(secret), httpx.ConnectError(secret)])
    monkeypatch.setattr(capture.asyncio, "sleep", AsyncMock())
    with pytest.raises(capture.CaptureError, match="after 2 attempt") as failure:
        await capture.fetch(monitor(retries=1), tmp_path)
    assert len(calls) == 2
    assert secret not in str(failure.value)
    assert secret not in caplog.text


def mock_browser(monkeypatch, *, goto_error=None, networkidle_error=None):
    async def screenshot(*, path, full_page, animations):
        assert full_page is True
        assert animations == "disabled"
        Image.new("RGB", (64, 64), color="white").save(path)

    page = SimpleNamespace(
        goto=AsyncMock(side_effect=goto_error, return_value=SimpleNamespace(status=200)),
        wait_for_selector=AsyncMock(),
        wait_for_load_state=AsyncMock(side_effect=networkidle_error),
        content=AsyncMock(return_value="<main>Rendered JS</main>"),
        screenshot=AsyncMock(side_effect=screenshot),
    )
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))
    playwright = SimpleNamespace(chromium=chromium)

    class AsyncManager:
        async def __aenter__(self):
            return playwright

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(capture, "async_playwright", AsyncManager)
    return page, browser, chromium


@pytest.mark.asyncio
async def test_browser_visible_mode_and_optional_proxy(monkeypatch, tmp_path):
    page, browser, chromium = mock_browser(monkeypatch)
    result = await capture.fetch(
        monitor(engine="playwright", headless=False, proxy="http://proxy.test:8080"), tmp_path
    )
    assert result.html == "<main>Rendered JS</main>"
    assert result.screenshot_path is None
    assert chromium.launch.await_args.kwargs == {
        "headless": False,
        "timeout": 5000,
        "proxy": {"server": "http://proxy.test:8080"},
    }
    assert browser.new_context.await_args.kwargs["user_agent"] == "Agent-A"
    assert page.goto.await_args.kwargs["wait_until"] == "domcontentloaded"
    page.screenshot.assert_not_awaited()
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_proxy_auth_is_passed_in_separate_fields(monkeypatch, tmp_path):
    _, _, chromium = mock_browser(monkeypatch)
    await capture.fetch(
        monitor(engine="playwright", proxy="http://user:p%40ss@proxy.test:8080"), tmp_path
    )
    assert chromium.launch.await_args.kwargs["proxy"] == {
        "server": "http://proxy.test:8080", "username": "user", "password": "p@ss"
    }


@pytest.mark.asyncio
async def test_js_target_is_waited_for_and_active_network_does_not_fail(monkeypatch, tmp_path):
    page, _, _ = mock_browser(
        monkeypatch, networkidle_error=capture.PlaywrightTimeoutError("tracking remains active")
    )
    result = await capture.fetch(
        monitor(engine="playwright", selector_type="css", selector="#price"), tmp_path
    )
    assert result.html == "<main>Rendered JS</main>"
    page.wait_for_selector.assert_awaited_once_with("#price", state="attached", timeout=5000)


@pytest.mark.asyncio
@pytest.mark.parametrize("option", ["visual", "image_hash"])
async def test_visual_flags_use_browser_and_unique_snapshots(monkeypatch, tmp_path, option):
    _, browser, _ = mock_browser(monkeypatch)
    monitored = monitor(id="../../unsafe id", **{option: True})
    first = await capture.fetch(monitored, tmp_path / "shots")
    second = await capture.fetch(monitored, tmp_path / "shots")
    assert first.screenshot_path != second.screenshot_path
    assert first.image_hash == second.image_hash
    assert len(first.image_hash) == 16
    assert len(list((tmp_path / "shots").glob("*.png"))) == 2
    assert browser.close.await_count == 2


@pytest.mark.asyncio
async def test_browser_failure_closes_browser(monkeypatch, tmp_path):
    _, browser, _ = mock_browser(monkeypatch, goto_error=RuntimeError("private endpoint"))
    with pytest.raises(capture.CaptureError, match="RuntimeError"):
        await capture.fetch(monitor(engine="playwright", retries=0), tmp_path)
    browser.close.assert_awaited_once()
    assert not list(tmp_path.glob("*.png"))
