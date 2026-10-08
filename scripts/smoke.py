"""Exercise real HTTP, Chromium, persistence, scheduler startup, and dashboard offline."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import shutil
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from PIL import Image
from playwright.async_api import async_playwright

from webchangesentinel.config import (
    AppConfig,
    FilterConfig,
    MonitorConfig,
    load_config,
    save_config,
)
from webchangesentinel.service import Sentinel
from webchangesentinel.web import create_app


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextmanager
def local_website():
    """Only this localhost website is monitored; no Internet destinations are needed."""
    state = {"text": "Precio inicial: 100", "layout": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            position = "bottom: 20px; right: 20px" if state["layout"] else "top: 20px; left: 20px"
            document = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>WebChangeSentinel smoke fixture</title><style>
body {{ margin: 0; font-family: sans-serif; background: white; }}
#content {{ padding: 24px; font-size: 28px; }}
#canvas {{ position: relative; width: 100%; height: 560px; border-top: 4px solid #333; }}
#shape {{ position: absolute; {position}; width: 460px; height: 240px; background: #152342; }}
</style></head><body><main><p id="content">{html.escape(state["text"])}</p>
<div id="canvas"><div id="shape"></div></div></main>
<script>setTimeout(() => {{ document.querySelector('#content').textContent += ' | JavaScript listo'; }}, 100);</script>
</body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def check_statuses(service: Sentinel, expected: dict[str, str]) -> None:
    for monitor_id, status in expected.items():
        outcome = await service.check(monitor_id)
        expect(outcome.status == status, f"{monitor_id}: expected {status}, got {outcome}")


async def validate_monitoring(config: AppConfig, state: dict) -> dict:
    service = Sentinel(config)
    ids = [monitor.id for monitor in config.monitors]
    try:
        await check_statuses(service, dict.fromkeys(ids, "baseline"))
        browser_snapshot = service.store.latest("browser")
        expect(
            "JavaScript listo" in browser_snapshot["text"],
            "Browser did not execute delayed JavaScript",
        )
        expect(
            "JavaScript listo" not in service.store.latest("css")["text"],
            "HTTP unexpectedly rendered JavaScript",
        )
        await check_statuses(service, dict.fromkeys(ids, "unchanged"))
        state["text"] = "Precio actualizado: 250000"
        await check_statuses(service, dict.fromkeys(ids, "changed"))
        state["layout"] = True
        await check_statuses(
            service,
            {"css": "unchanged", "xpath": "unchanged", "full": "unchanged", "browser": "changed"},
        )
        summaries = {}
        for monitor_id in ids:
            snapshots = service.store.history(monitor_id)
            events = service.store.events(monitor_id)
            expect(len(snapshots) == 4, f"{monitor_id}: expected 4 snapshots")
            expect(
                len(events) == (2 if monitor_id == "browser" else 1),
                f"{monitor_id}: incorrect event history",
            )
            expect(
                all(len(item["content_hash"]) == 64 for item in snapshots),
                f"{monitor_id}: invalid SHA-256",
            )
            expect(
                all("<script" not in item["clean_html"] for item in snapshots),
                f"{monitor_id}: unclean HTML",
            )
            expect(
                "Precio actualizado" in events[-1]["diff"], f"{monitor_id}: missing textual diff"
            )
            summaries[monitor_id] = {"snapshots": len(snapshots), "events": len(events)}
        visual_event = service.store.events("browser")[0]
        expect(
            visual_event["difference_percent"] == 0, "Visual-only change unexpectedly altered text"
        )
        expect(visual_event["visual_difference_percent"] > 0, "Visual change was not detected")
        for snapshot in service.store.history("browser"):
            image_path = Path(snapshot["screenshot_path"])
            expect(image_path.is_file(), "Screenshot missing on disk")
            expect(len(snapshot["image_hash"]) == 16, "Perceptual hash must contain 64 bits")
            int(snapshot["image_hash"], 16)
            with Image.open(image_path) as image:
                image.verify()
        summaries["visual_difference_percent"] = visual_event["visual_difference_percent"]
        return summaries
    finally:
        service.close()


async def validate_dashboard(
    config: AppConfig, path: Path, target_url: str, temporary: Path, artifacts: Path | None
) -> dict:
    app = create_app(path, config)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError("Dashboard stopped before startup")
            await asyncio.sleep(0.025)
        expect(server.started, "Dashboard did not become ready within 5 seconds")
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as client:
            health = await client.get("/health")
            expect(
                health.status_code == 200 and health.json()["status"] == "ok",
                "Health endpoint failed",
            )
            dashboard = await client.get("/")
            expect(
                dashboard.status_code == 200 and "Mantente al tanto" in dashboard.text,
                "Dashboard HTML failed",
            )
            response = await client.post("/api/monitors/css/check")
            expect(
                response.status_code == 200 and response.json()["status"] == "unchanged",
                "API manual check failed",
            )
            history = await client.get("/api/monitors/css/history")
            expect(
                len(history.json()["snapshots"]) == 5, "API history does not reflect manual check"
            )
            monitor = {"id": "api-smoke", "url": target_url, "enabled": False, "interval": "2h"}
            expect(
                (await client.post("/api/monitors", json=monitor)).status_code == 201,
                "API create failed",
            )
            monitor["name"] = "Monitor actualizado por API"
            expect(
                (await client.put("/api/monitors/api-smoke", json=monitor)).status_code == 200,
                "API edit failed",
            )
            expect(
                load_config(path).monitors[-1].name == monitor["name"],
                "API edit was not saved to YAML",
            )
            expect(
                (await client.delete("/api/monitors/api-smoke")).status_code == 204,
                "API delete failed",
            )
            rows = (await client.get("/api/monitors")).json()
            expect(len(rows) == 4, "API delete left an active monitor")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(base, wait_until="networkidle")
                expect(
                    "La web cambia." in await page.locator("h1").inner_text(),
                    "Dashboard heading did not render",
                )
                expect(
                    await page.locator(".monitor-card").count() == 4,
                    "Dashboard monitors did not render",
                )
                expect(
                    await page.evaluate("typeof htmx !== 'undefined'"),
                    "Local HTMX script did not load",
                )
                expect(not errors, f"Dashboard JavaScript errors: {errors}")
                desktop = temporary / "dashboard-desktop.png"
                await page.screenshot(path=str(desktop), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.goto(base, wait_until="networkidle")
                expect(
                    await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"),
                    "Mobile dashboard overflows horizontally",
                )
                mobile = temporary / "dashboard-mobile.png"
                await page.screenshot(path=str(mobile), full_page=True)
                await page.goto(f"{base}/monitors/browser", wait_until="networkidle")
                expect(
                    "Eventos recientes" in await page.locator("main").inner_text(),
                    "Dashboard history did not render",
                )
                expect(
                    await page.locator("img.screenshot").count() == 4,
                    "Visual history screenshots did not render",
                )
                if artifacts is not None:
                    artifacts.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(desktop, artifacts / desktop.name)
                    shutil.copy2(mobile, artifacts / mobile.name)
            finally:
                await browser.close()
        return {"health": "ok", "api_crud": "ok", "rendered_monitors": 4, "mobile_overflow": False}
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        listener.close()


async def smoke(artifacts: Path | None) -> None:
    with (
        TemporaryDirectory(prefix="webchangesentinel-smoke-") as directory,
        local_website() as (url, state),
    ):
        temporary = Path(directory)
        filters = FilterConfig(threshold_percent=0, min_changed_chars=1)
        common = {
            "url": url,
            "enabled": False,
            "interval": "1h",
            "filters": filters,
            "retries": 0,
            "timeout": 10,
        }
        config = AppConfig(
            database_url=f"sqlite:///{temporary / 'sentinel.db'}",
            snapshot_dir=temporary / "screenshots",
            monitors=[
                MonitorConfig(id="css", selector_type="css", selector="#content", **common),
                MonitorConfig(
                    id="xpath",
                    selector_type="xpath",
                    selector="//*[@id='content']/text()",
                    **common,
                ),
                MonitorConfig(id="full", selector_type="full", **common),
                MonitorConfig(
                    id="browser",
                    selector_type="css",
                    selector="#content",
                    engine="playwright",
                    visual=True,
                    **common,
                ),
            ],
        )
        path = temporary / "config.yaml"
        save_config(config, path)
        monitoring = await validate_monitoring(config, state)
        dashboard = await validate_dashboard(config, path, url, temporary, artifacts)
        print(
            json.dumps(
                {"result": "passed", "monitoring": monitoring, "dashboard": dashboard},
                ensure_ascii=False,
                indent=2,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke real offline de WebChangeSentinel con Chromium"
    )
    parser.add_argument(
        "--artifacts", type=Path, help="Conservar screenshots desktop/mobile en este directorio"
    )
    args = parser.parse_args()
    asyncio.run(smoke(args.artifacts))


if __name__ == "__main__":
    main()
