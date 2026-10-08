import pytest
import yaml
from pydantic import ValidationError

from webchangesentinel.config import (
    AppConfig,
    FilterConfig,
    MonitorConfig,
    interval_seconds,
    load_config,
    save_config,
)


@pytest.mark.parametrize(
    "value,seconds", [("30s", 30), ("5m", 300), ("1.5h", 5400), ("2d", 172800)]
)
def test_intervals(value, seconds):
    assert interval_seconds(value) == seconds


@pytest.mark.parametrize("value", ["0m", "forever", "-1h", "9999d", "0.1s"])
def test_invalid_intervals(value):
    with pytest.raises(ValueError):
        interval_seconds(value)


def test_config_rejects_duplicate_and_unknown_channel():
    monitor = MonitorConfig(id="a", url="https://example.com")
    with pytest.raises(ValidationError):
        AppConfig(monitors=[monitor, monitor])
    with pytest.raises(ValidationError):
        AppConfig(monitors=[monitor.model_copy(update={"channels": ["missing"]})])


def test_selector_and_patterns_validated():
    with pytest.raises(ValidationError):
        MonitorConfig(id="bad", url="https://example.com", selector_type="xpath", selector="[[")
    with pytest.raises(ValidationError):
        MonitorConfig(id="bad", url="file:///etc/passwd")
    with pytest.raises(ValidationError):
        FilterConfig(ignore_patterns=["["])
    with pytest.raises(ValidationError):
        MonitorConfig(id="bad", url="https://example.com", selector_type="css")


def test_environment_references_survive_crud_reordering(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK", "https://hooks.slack.com/secret")
    monkeypatch.setenv("PROXY", "http://user:password@localhost:9999")
    path = tmp_path / "config.yaml"
    path.write_text(
        "notifications:\n  slack:\n    kind: slack\n    webhook_url: ${WEBHOOK}\nmonitors:\n  - id: first\n    url: https://example.com\n    proxy: ${PROXY}\n  - id: second\n    url: https://example.org\n"
    )
    config = load_config(path)
    assert config.notifications["slack"].webhook_url.endswith("secret")
    updated = config.model_copy(update={"monitors": list(reversed(config.monitors))})
    save_config(updated, path)
    text = path.read_text()
    assert "${WEBHOOK}" in text and "${PROXY}" in text
    assert "password" not in yaml.safe_load(text)["monitors"][1]["proxy"]
    assert "hooks.slack.com" not in text
    assert load_config(path).monitors[1].proxy == "http://user:password@localhost:9999"


def test_missing_variable_has_no_value_leak(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("database_url: ${SENTINEL_NOT_SET_ABC123}\n")
    with pytest.raises(ValueError, match="SENTINEL_NOT_SET_ABC123"):
        load_config(path)


def test_save_revalidates_model_copy_and_keeps_previous_file(tmp_path):
    path = tmp_path / "config.yaml"
    save_config(AppConfig(), path)
    previous = path.read_bytes()
    with pytest.raises(ValidationError):
        save_config(AppConfig().model_copy(update={"concurrency": 0}), path)
    assert path.read_bytes() == previous


def test_normalized_environment_values_not_written_to_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("TARGET", "https://example.test?token=synthetic-secret")
    monkeypatch.setenv("SMTP_PORT", "587")
    path = tmp_path / "config.yaml"
    path.write_text(
        "monitors:\n  - id: target\n    url: ${TARGET}\nnotifications:\n  mail:\n    kind: email\n    port: ${SMTP_PORT}\n    from_address: test@example.com\n    to_addresses: [test@example.com]\n"
    )
    config = load_config(path)
    assert config.monitors[0].url != "https://example.test?token=synthetic-secret"
    save_config(
        config.model_copy(
            update={"monitors": [config.monitors[0].model_copy(update={"name": "Edited"})]}
        ),
        path,
    )
    text = path.read_text()
    assert "${TARGET}" in text and "${SMTP_PORT}" in text
    assert "synthetic-secret" not in text
    assert load_config(path).monitors[0].name == "Edited"


def test_empty_keyword_and_invalid_proxy_rejected():
    with pytest.raises(ValidationError):
        FilterConfig(keywords=[" "])
    with pytest.raises(ValidationError):
        MonitorConfig(id="invalid", url="https://example.com", proxy="not-a-proxy")


def test_environment_monitor_id_preserves_nested_secrets_when_reordered(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_ID", "dynamic-id")
    monkeypatch.setenv("TARGET_URL", "https://example.test?token=synthetic-secret")
    monkeypatch.setenv("MONITOR_PROXY", "http://user:synthetic-password@localhost:9999")
    path = tmp_path / "config.yaml"
    path.write_text(
        "monitors:\n  - id: ${MONITOR_ID}\n    url: ${TARGET_URL}\n    proxy: ${MONITOR_PROXY}\n  - id: other\n    url: https://example.org\n"
    )
    config = load_config(path)
    save_config(config.model_copy(update={"monitors": list(reversed(config.monitors))}), path)
    text = path.read_text()
    assert "${MONITOR_ID}" in text and "${TARGET_URL}" in text and "${MONITOR_PROXY}" in text
    assert "synthetic-secret" not in text and "synthetic-password" not in text
    assert load_config(path).monitors[1].id == "dynamic-id"
