from webchangesentinel.cli import main
from webchangesentinel.config import AppConfig, MonitorConfig, save_config


def test_init_validate_list_and_no_overwrite(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    argv = ["--config", str(path)]
    assert main([*argv, "init"]) == 0
    before = path.read_bytes()
    assert main([*argv, "init"]) == 2
    assert path.read_bytes() == before
    assert main([*argv, "validate"]) == 0
    assert main([*argv, "list"]) == 0
    assert "Configuración válida" in capsys.readouterr().out


def test_unknown_monitor_and_invalid_config(tmp_path):
    path = tmp_path / "config.yaml"
    save_config(AppConfig(monitors=[MonitorConfig(id="test", url="https://example.com")]), path)
    assert main(["--config", str(path), "check", "missing"]) == 2
    path.write_text("concurrency: 0\n")
    assert main(["--config", str(path), "validate"]) == 2


def test_cli_check_reports_real_service_failure(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    config = AppConfig(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        monitors=[MonitorConfig(id="test", url="https://example.com", retries=0)],
    )
    save_config(config, path)

    async def failed(*args):
        raise RuntimeError("private token")

    monkeypatch.setattr("webchangesentinel.service.fetch", failed)
    assert main(["--config", str(path), "check", "test"]) == 1
    output = capsys.readouterr().out
    assert '"status": "error"' in output and "private token" not in output
