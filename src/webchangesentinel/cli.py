"""Command-line entry point. All state-changing actions share the application service."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from dataclasses import asdict
from pathlib import Path

import uvicorn
import yaml
from pydantic import ValidationError

from .config import AppConfig, load_config
from .scheduler import MonitorScheduler
from .service import Sentinel


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # These libraries log request URLs, which can contain bot/webhook credentials.
    for name in ("httpx", "httpcore", "telegram", "apscheduler", "sqlalchemy"):
        logging.getLogger(name).setLevel(logging.WARNING)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="WebChangeSentinel: monitoreo de cambios web")
    result.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("WEBCHANGESENTINEL_CONFIG", "config.yaml")),
    )
    result.add_argument("--verbose", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Crear configuración vacía sin sobrescribir archivos")
    commands.add_parser("validate", help="Validar YAML, intervalos, selectores y canales")
    commands.add_parser("list", help="Listar monitores configurados")
    check = commands.add_parser("check", help="Capturar un monitor, o todos los habilitados")
    check.add_argument("id", nargs="?")
    check.add_argument(
        "--visible", action="store_true", help="Abrir Chromium visible (requiere display)"
    )
    commands.add_parser("run", help="Ejecutar el scheduler hasta SIGINT/SIGTERM")
    serve = commands.add_parser("serve", help="Ejecutar dashboard y scheduler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return result


async def _check(config: AppConfig, monitor_id: str | None) -> int:
    service = Sentinel(config)
    try:
        outcomes = [await service.check(monitor_id)] if monitor_id else await service.check_all()
        for outcome in outcomes:
            print(json.dumps(asdict(outcome), ensure_ascii=False))
        return int(
            any(
                outcome.status == "error" or outcome.error or outcome.delivery_errors
                for outcome in outcomes
            )
        )
    finally:
        service.close()


async def _run(config: AppConfig) -> None:
    service = Sentinel(config)
    scheduler = MonitorScheduler(service)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    try:
        scheduler.start()
        logging.getLogger(__name__).info(
            "Scheduler started with %d enabled monitors", sum(m.enabled for m in config.monitors)
        )
        await stop.wait()
    finally:
        scheduler.stop()
        await asyncio.sleep(0)
        service.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    configure_logging(args.verbose)
    try:
        if args.command == "init":
            args.config.parent.mkdir(parents=True, exist_ok=True)
            with args.config.open("x", encoding="utf-8") as stream:
                yaml.safe_dump(AppConfig().model_dump(mode="json"), stream, sort_keys=False)
            print(f"Configuración creada: {args.config}")
            return 0
        config = load_config(args.config)
        if args.command == "validate":
            print(
                f"Configuración válida: {len(config.monitors)} monitores, {len(config.notifications)} canales"
            )
        elif args.command == "list":
            for monitor in config.monitors:
                print(
                    f"{monitor.id}\t{'activo' if monitor.enabled else 'pausado'}\t{monitor.interval}\t{monitor.url}"
                )
        elif args.command == "check":
            if args.id and args.id not in {monitor.id for monitor in config.monitors}:
                print("Monitor no encontrado")
                return 2
            if args.visible:
                config = config.model_copy(
                    update={
                        "monitors": [
                            monitor.model_copy(update={"headless": False, "engine": "playwright"})
                            for monitor in config.monitors
                        ]
                    }
                )
            return asyncio.run(_check(config, args.id))
        elif args.command == "run":
            asyncio.run(_run(config))
        elif args.command == "serve":
            from .web import create_app

            uvicorn.run(
                create_app(args.config, config),
                host=args.host,
                port=args.port,
                workers=1,
                log_level="debug" if args.verbose else "info",
            )
        return 0
    except KeyboardInterrupt:
        return 130
    except FileExistsError:
        print("El archivo de configuración ya existe; init no lo sobrescribe")
    except FileNotFoundError:
        print("No se encontró la configuración. Ejecuta init o usa --config")
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False, include_context=False):
            print(f"Configuración inválida ({'.'.join(map(str, error['loc']))}): {error['msg']}")
    except ValueError as exc:
        # Configuration validators use constant messages; YAML parse errors may include secrets.
        if isinstance(exc, yaml.YAMLError):
            print("YAML inválido; revisa el formato del archivo")
        else:
            print(f"Configuración inválida: {exc}")
    except Exception as exc:
        print(
            f"No se pudo completar la operación ({type(exc).__name__}); revisa configuración y permisos"
        )
    return 2
