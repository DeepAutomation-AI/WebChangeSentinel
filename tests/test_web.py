"""Dashboard integration tests use a real database and mocked capture adapters."""

import pytest
from fastapi.testclient import TestClient

from webchangesentinel.capture import FetchedPage
from webchangesentinel.config import AppConfig, MonitorConfig, load_config, save_config
from webchangesentinel.storage import Snapshot
from webchangesentinel.web import create_app


@pytest.fixture
def dashboard(tmp_path):
    config = AppConfig(
        database_url=f"sqlite:///{tmp_path / 'sentinel.db'}",
        snapshot_dir=tmp_path / "screenshots",
        monitors=[MonitorConfig(id="prices", url="https://example.com/prices", enabled=False)],
    )
    path = tmp_path / "config.yaml"
    save_config(config, path)
    app = create_app(path, config)
    with TestClient(app) as client:
        yield client, app, path


def test_dashboard_and_local_assets_work_without_external_javascript(dashboard):
    client, _, _ = dashboard
    response = client.get("/")
    assert response.status_code == 200
    assert "Mantente al tanto" in response.text
    assert "https://example.com/prices" in response.text
    assert "Pausado" in response.text
    assert "/static/htmx.min.js" in response.text
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/health").json() == {"status": "ok", "monitors": 1}
    assert client.get("/monitors/prices").status_code == 200
    assert client.get("/monitors/missing").status_code == 404


def test_api_crud_persists_yaml_and_syncs_scheduled_jobs(dashboard):
    client, app, path = dashboard
    monitor = {"id": "news", "name": "Noticias", "url": "https://example.com/news", "interval": "2h"}
    response = client.post("/api/monitors", json=monitor)
    assert response.status_code == 201
    assert [item.id for item in load_config(path).monitors] == ["prices", "news"]
    job = app.state.scheduler.scheduler.get_job("news")
    assert job.trigger.interval.total_seconds() == 7200
    monitor.update(enabled=False, interval="1d")
    assert client.put("/api/monitors/news", json=monitor).status_code == 200
    assert app.state.scheduler.scheduler.get_job("news") is None
    assert load_config(path).monitors[-1].interval == "1d"
    assert client.delete("/api/monitors/news").status_code == 204
    assert [item["id"] for item in client.get("/api/monitors").json()] == ["prices"]
    assert [item.id for item in load_config(path).monitors] == ["prices"]


@pytest.mark.parametrize("overrides", [
    {"interval": "0m"}, {"selector_type": "css", "selector": "["},
    {"selector_type": "xpath", "selector": "//*["}, {"url": "javascript:alert(1)"},
    {"channels": ["unconfigured"]},
])
def test_invalid_api_configuration_does_not_modify_disk(dashboard, overrides):
    client, _, path = dashboard
    before = path.read_text()
    data = {"id": "invalid", "url": "https://example.com", **overrides}
    assert client.post("/api/monitors", json=data).status_code == 422
    assert path.read_text() == before
    assert len(client.get("/api/monitors").json()) == 1


def test_duplicate_and_identity_changes_are_rejected(dashboard):
    client, _, _ = dashboard
    data = {"id": "prices", "url": "https://example.com"}
    assert client.post("/api/monitors", json=data).status_code == 409
    data["id"] = "another"
    assert client.put("/api/monitors/prices", json=data).status_code == 422
    assert client.delete("/api/monitors/unknown").status_code == 404


def test_manual_check_builds_history_and_safely_displays_diff(dashboard, monkeypatch):
    client, _, _ = dashboard
    pages = iter(["<p>Precio: 100</p>", "<p>Precio: 200 &lt;script&gt;alert(1)&lt;/script&gt;</p>"])

    async def fake_fetch(*_):
        return FetchedPage(next(pages))

    monkeypatch.setattr("webchangesentinel.service.fetch", fake_fetch)
    assert client.post("/api/monitors/prices/check").json()["status"] == "baseline"
    assert client.post("/api/monitors/prices/check").json()["status"] == "changed"
    history = client.get("/api/monitors/prices/history").json()
    assert len(history["snapshots"]) == 2
    assert len(history["events"]) == 1
    assert "Precio: 200" in history["events"][0]["diff"]
    html = client.get("/monitors/prices").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert client.get("/api/monitors/prices/history?limit=0").status_code == 422
    assert client.get("/api/monitors/unknown/history").status_code == 404
    assert client.post("/api/monitors/unknown/check").status_code == 404


def test_monitor_names_are_escaped_and_proxy_credentials_are_never_exposed(dashboard):
    client, app, _ = dashboard
    secret_proxy = "http://proxy-user:proxy-secret@proxy.example:8000"
    data = {"id": "escape", "name": "<script>alert(9)</script>", "url": "https://example.com", "enabled": False, "proxy": secret_proxy}
    response = client.post("/api/monitors", json=data)
    assert response.status_code == 201
    assert "proxy" not in response.json()
    assert "proxy-secret" not in client.get("/api/monitors").text
    for endpoint in ("/", "/partials/monitors", "/monitors/escape"):
        html = client.get(endpoint).text
        assert "<script>alert(9)</script>" not in html
        assert "&lt;script&gt;alert(9)&lt;/script&gt;" in html
        assert "proxy-secret" not in html
    del data["proxy"]
    assert client.put("/api/monitors/escape", json=data).status_code == 200
    assert next(item for item in app.state.service.config.monitors if item.id == "escape").proxy == secret_proxy


def test_html_forms_create_edit_toggle_and_delete(dashboard):
    client, _, path = dashboard
    form = {"id": "form", "name": "Formulario", "url": "https://example.com", "interval": "1h", "engine": "http", "selector_type": "full", "enabled": "on", "headless": "on"}
    assert client.post("/monitors", data=form).status_code == 200
    assert len(load_config(path).monitors) == 2
    form["name"] = "Editado"
    assert client.post("/monitors/form/edit", data=form).status_code == 200
    assert load_config(path).monitors[-1].name == "Editado"
    response = client.post("/monitors/form/toggle", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Pausado" in response.text
    assert load_config(path).monitors[-1].enabled is False
    assert client.post("/monitors/form/delete").status_code == 200
    assert len(load_config(path).monitors) == 1


def test_invalid_html_form_shows_validation_without_persisting(dashboard):
    client, _, path = dashboard
    before = path.read_text()
    response = client.post("/monitors", data={"id": "bad", "url": "https://example.com", "interval": "yesterday", "engine": "http", "selector_type": "full"})
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "yesterday" in response.text
    assert path.read_text() == before


def test_cross_site_browser_writes_are_rejected(dashboard):
    client, _, _ = dashboard
    headers = {"Origin": "https://untrusted.example", "Sec-Fetch-Site": "cross-site"}
    assert client.post("/monitors/prices/toggle", headers=headers).status_code == 403
    assert client.delete("/api/monitors/prices", headers=headers).status_code == 403
    assert client.get("/api/monitors").json()[0]["enabled"] is False


def test_screenshot_serving_rejects_files_outside_snapshot_directory(dashboard, tmp_path):
    client, app, _ = dashboard
    root = app.state.service.config.snapshot_dir
    root.mkdir()
    screenshot = root / "capture.png"
    screenshot.write_bytes(b"PNG-test")
    outside = tmp_path / "private.txt"
    outside.write_text("must not be exposed")
    ids = []
    with app.state.service.store.sessions.begin() as session:
        for image_path in (screenshot, outside):
            snapshot = Snapshot(monitor_id="prices", content_hash="x" * 64, clean_html="<p>test</p>", text="test", screenshot_path=str(image_path))
            session.add(snapshot)
            session.flush()
            ids.append(snapshot.id)
    assert client.get(f"/snapshots/{ids[0]}/image").content == b"PNG-test"
    assert client.get(f"/snapshots/{ids[1]}/image").status_code == 404
    assert client.get("/snapshots/999/image").status_code == 404


def test_dashboard_preserves_env_references_when_saving(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_PROXY", "http://proxy-user:proxy-secret@proxy.example:8000")
    path = tmp_path / "config.yaml"
    path.write_text(f"database_url: sqlite:///{tmp_path / 'refs.db'}\nmonitors:\n  - id: private\n    url: https://example.com\n    enabled: false\n    proxy: ${{SENTINEL_PROXY}}\n")
    app = create_app(path)
    with TestClient(app) as client:
        response = client.post("/monitors/private/toggle")
        assert response.status_code == 200
    persisted = path.read_text()
    assert "${SENTINEL_PROXY}" in persisted
    assert "proxy-secret" not in persisted
