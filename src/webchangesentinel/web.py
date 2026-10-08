"""Local FastAPI dashboard and REST API for monitor configuration and history."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .config import AppConfig, MonitorConfig, load_config, save_config
from .scheduler import MonitorScheduler
from .service import Sentinel

PACKAGE_DIR = Path(__file__).parent
STATUS_LABELS = {
    "baseline": "Primera captura", "unchanged": "Sin cambios", "filtered": "Filtrado",
    "changed": "Cambio detectado", "error": "Error", "busy": "En ejecución",
    "pending": "Pendiente", "disabled": "Pausado", "ok": "Activo",
}


def _public_monitor(monitor: MonitorConfig) -> dict[str, Any]:
    # Proxies may contain credentials. Notification settings never enter this API.
    return monitor.model_dump(mode="json", exclude={"proxy"})


def _same_origin(request: Request) -> None:
    """Reject browser writes originating at another website, including form posts."""
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Solicitud de otro sitio rechazada")
    parsed_origin = urlsplit(origin) if origin else None
    if parsed_origin and (parsed_origin.scheme, parsed_origin.netloc) != (request.url.scheme, request.url.netloc):
        raise HTTPException(403, "Origen de solicitud no permitido")


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_input=False)
    )


def create_app(
    config_path: str | Path = "config.yaml", config: AppConfig | None = None
) -> FastAPI:
    """Create an independently owned service; workers must not share its scheduler."""
    path = Path(config_path)
    service = Sentinel(config if config is not None else load_config(path))
    scheduler = MonitorScheduler(service)
    mutation_lock = asyncio.Lock()
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.globals["status_labels"] = STATUS_LABELS

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            service.close()

    app = FastAPI(title="WebChangeSentinel", version="1.0.0", lifespan=lifespan)
    app.state.service = service
    app.state.scheduler = scheduler
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    def monitor_by_id(monitor_id: str) -> MonitorConfig:
        for monitor in service.config.monitors:
            if monitor.id == monitor_id:
                return monitor
        raise HTTPException(404, "Monitor no encontrado")

    def monitor_rows() -> list[dict[str, Any]]:
        states = {row["id"]: row for row in service.store.list_monitors()}
        rows = []
        for monitor in service.config.monitors:
            state = states.get(monitor.id, {})
            row = _public_monitor(monitor)
            row["state"] = state
            row["status"] = state.get("status", state.get("last_status", "pending"))
            if not monitor.enabled:
                row["status"] = "disabled"
            row["last_checked_at"] = state.get("last_checked_at", state.get("last_checked"))
            row["last_error"] = state.get("last_error")
            rows.append(row)
        return rows

    def render(request: Request, template: str, *, status_code: int = 200, **context):
        return templates.TemplateResponse(
            request=request, name=template, context=context, status_code=status_code
        )

    async def persist(monitors: list[MonitorConfig]) -> None:
        updated = service.config.model_copy(update={"monitors": monitors})
        try:
            AppConfig.model_validate(updated.model_dump())
            save_config(updated, path)
        except ValidationError as exc:
            raise HTTPException(422, _validation_message(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, "No se pudo guardar la configuración") from exc
        service.reload_config(updated)
        scheduler.sync_jobs()

    async def upsert(monitor: MonitorConfig, *, creating: bool) -> None:
        async with mutation_lock:
            existing = [item for item in service.config.monitors if item.id == monitor.id]
            if creating and existing:
                raise HTTPException(409, "Ya existe un monitor con ese identificador")
            if not creating and not existing:
                raise HTTPException(404, "Monitor no encontrado")
            monitors = [monitor if item.id == monitor.id else item for item in service.config.monitors]
            if creating:
                monitors.append(monitor)
            await persist(monitors)

    async def remove(monitor_id: str) -> None:
        async with mutation_lock:
            monitor_by_id(monitor_id)
            await persist([item for item in service.config.monitors if item.id != monitor_id])

    async def parse_monitor_form(request: Request, existing: MonitorConfig | None = None):
        _same_origin(request)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Se requiere un formulario URL-encoded")
        body = await request.body()
        if len(body) > 65536:
            raise HTTPException(413, "Formulario demasiado grande")
        try:
            fields = {key: values[-1] for key, values in parse_qs(body.decode(), keep_blank_values=True).items()}
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "Formulario con codificación UTF-8 inválida") from exc
        data = existing.model_dump() if existing else {}
        for key in ("id", "name", "url", "selector_type", "engine", "interval"):
            if key in fields:
                data[key] = fields[key].strip()
        if existing:
            data["id"] = existing.id
        data["selector"] = fields.get("selector", "").strip() or None
        for key in ("enabled", "headless", "visual", "image_hash"):
            data[key] = fields.get(key) == "on"
        data["channels"] = [entry.strip() for entry in fields.get("channels", "").split(",") if entry.strip()]
        filters = data.get("filters", {})
        for key in ("threshold_percent", "min_changed_chars"):
            if fields.get(key):
                filters[key] = fields[key]
        for key in ("keywords", "ignore_selectors", "ignore_patterns"):
            filters[key] = [line.strip() for line in fields.get(key, "").splitlines() if line.strip()]
        filters["ignore_case"] = fields.get("ignore_case") == "on"
        data["filters"] = filters
        try:
            return MonitorConfig.model_validate(data), data, None
        except ValidationError as exc:
            return None, data, _validation_message(exc)

    def empty_form() -> dict[str, Any]:
        return {
            "id": "", "name": "", "url": "", "selector": "", "selector_type": "full",
            "engine": "http", "interval": "5m", "enabled": True, "headless": True,
            "visual": False, "image_hash": False, "channels": [],
            "filters": {"threshold_percent": 1, "min_changed_chars": 1, "keywords": [],
                        "ignore_selectors": [], "ignore_patterns": [], "ignore_case": False},
        }

    @app.get("/health")
    async def health():
        return {"status": "ok", "monitors": len(service.config.monitors)}

    @app.get("/api/monitors")
    async def api_monitors():
        return monitor_rows()

    @app.post("/api/monitors", status_code=201)
    async def api_create(request: Request, monitor: MonitorConfig):
        _same_origin(request)
        await upsert(monitor, creating=True)
        return _public_monitor(monitor)

    @app.put("/api/monitors/{monitor_id}")
    async def api_update(request: Request, monitor_id: str, monitor: MonitorConfig):
        _same_origin(request)
        if monitor.id != monitor_id:
            raise HTTPException(422, "El identificador no se puede cambiar")
        # A redacted GET response can safely round-trip without losing proxy settings.
        if "proxy" not in monitor.model_fields_set:
            monitor = monitor.model_copy(update={"proxy": monitor_by_id(monitor_id).proxy})
        await upsert(monitor, creating=False)
        return _public_monitor(monitor)

    @app.delete("/api/monitors/{monitor_id}", status_code=204)
    async def api_delete(request: Request, monitor_id: str):
        _same_origin(request)
        await remove(monitor_id)

    @app.post("/api/monitors/{monitor_id}/check")
    async def api_check(request: Request, monitor_id: str):
        _same_origin(request)
        monitor_by_id(monitor_id)
        outcome = await service.check(monitor_id)
        return asdict(outcome) if is_dataclass(outcome) else outcome

    @app.get("/api/monitors/{monitor_id}/history")
    async def api_history(monitor_id: str, limit: int = 50):
        monitor_by_id(monitor_id)
        if not 1 <= limit <= 500:
            raise HTTPException(422, "El límite debe estar entre 1 y 500")
        return {"snapshots": service.store.history(monitor_id, limit=limit),
                "events": service.store.events(monitor_id, limit=limit)}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        rows = monitor_rows()
        return render(request, "index.html", monitors=rows,
                      active_count=sum(row["enabled"] for row in rows), error=None,
                      form=empty_form(), editing=False)

    @app.get("/partials/monitors", response_class=HTMLResponse)
    async def monitor_partial(request: Request):
        return render(request, "monitor_list.html", monitors=monitor_rows())

    @app.post("/monitors", response_class=HTMLResponse)
    async def form_create(request: Request):
        monitor, fields, error = await parse_monitor_form(request)
        if monitor is not None:
            try:
                await upsert(monitor, creating=True)
            except HTTPException as exc:
                error = exc.detail
        if error:
            rows = monitor_rows()
            return render(request, "index.html", monitors=rows,
                          active_count=sum(row["enabled"] for row in rows), error=error,
                          form=empty_form() | fields, editing=False, status_code=422)
        return RedirectResponse("/", status_code=303)

    @app.get("/monitors/{monitor_id}", response_class=HTMLResponse)
    async def detail(request: Request, monitor_id: str):
        monitor = monitor_by_id(monitor_id)
        return render(request, "detail.html", monitor=_public_monitor(monitor),
                      form=_public_monitor(monitor), editing=True, error=None,
                      snapshots=service.store.history(monitor_id),
                      events=service.store.events(monitor_id))

    @app.post("/monitors/{monitor_id}/edit", response_class=HTMLResponse)
    async def form_update(request: Request, monitor_id: str):
        existing = monitor_by_id(monitor_id)
        monitor, fields, error = await parse_monitor_form(request, existing)
        if monitor is not None:
            try:
                await upsert(monitor, creating=False)
                return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)
            except HTTPException as exc:
                error = exc.detail
        return render(request, "detail.html", monitor=_public_monitor(existing),
                      form=_public_monitor(existing) | fields, editing=True, error=error,
                      snapshots=service.store.history(monitor_id), events=service.store.events(monitor_id),
                      status_code=422)

    @app.post("/monitors/{monitor_id}/toggle", response_class=HTMLResponse)
    async def form_toggle(request: Request, monitor_id: str):
        _same_origin(request)
        async with mutation_lock:
            monitor = monitor_by_id(monitor_id)
            updated = monitor.model_copy(update={"enabled": not monitor.enabled})
            await persist([updated if item.id == monitor_id else item for item in service.config.monitors])
        if request.headers.get("hx-request") == "true":
            return render(request, "monitor_list.html", monitors=monitor_rows())
        return RedirectResponse("/", status_code=303)

    @app.post("/monitors/{monitor_id}/delete", response_class=HTMLResponse)
    async def form_delete(request: Request, monitor_id: str):
        _same_origin(request)
        await remove(monitor_id)
        if request.headers.get("hx-request") == "true":
            return render(request, "monitor_list.html", monitors=monitor_rows())
        return RedirectResponse("/", status_code=303)

    @app.post("/monitors/{monitor_id}/check", response_class=HTMLResponse)
    async def form_check(request: Request, monitor_id: str):
        _same_origin(request)
        monitor_by_id(monitor_id)
        outcome = await service.check(monitor_id)
        if request.headers.get("hx-request") == "true":
            return render(request, "check_result.html", outcome=outcome)
        return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)

    @app.get("/snapshots/{snapshot_id}/image")
    async def snapshot_image(snapshot_id: int):
        snapshot = service.store.snapshot(snapshot_id)
        if not snapshot or not snapshot.get("screenshot_path"):
            raise HTTPException(404, "Captura visual no disponible")
        image_path = Path(snapshot["screenshot_path"]).resolve()
        root = service.config.snapshot_dir.resolve()
        if not image_path.is_relative_to(root) or not image_path.is_file():
            raise HTTPException(404, "Captura visual no disponible")
        return FileResponse(image_path, media_type="image/png")

    return app
