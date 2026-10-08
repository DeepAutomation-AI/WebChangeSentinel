# WebChangeSentinel

Sistema de monitoreo de cambios web en Python. Guarda capturas en SQLite o PostgreSQL, compara el contenido entre comprobaciones y envía alertas cuando un cambio supera los filtros de cada monitor.

## Funcionalidades

- Múltiples URLs con selección CSS, XPath o texto completo.
- Descarga HTTP con httpx y BeautifulSoup; renderizado JavaScript con Playwright/Chromium.
- Comparación textual con `difflib`, capturas visuales y comparación de imágenes mediante hash perceptual.
- Frecuencias por monitor en segundos, minutos, horas o días, con APScheduler.
- Snapshots con HTML limpio, texto, hash e historial de cambios.
- Alertas SMTP, Telegram, Discord, Slack y escritorio.
- Umbral porcentual, mínimo de caracteres cambiados, palabras clave, selectores y expresiones regulares para ignorar contenido.
- Dashboard FastAPI con HTMX para configurar monitores, comprobarlos y consultar el historial.
- Reintentos con backoff exponencial, rotación de user agents, proxy opcional y navegador visible para depurar.
- Configuración YAML, CLI, Docker Compose y pruebas con pytest y dobles de HTTP/Playwright.

## Inicio rápido

Se necesita Python 3.11 o posterior y [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/DeepAutomation-AI/WebChangeSentinel.git
cd WebChangeSentinel
uv sync --extra dev --frozen
cp config.example.yaml config.yaml
uv run webchangesentinel --config config.yaml validate
uv run webchangesentinel --config config.yaml serve
```

Abre <http://127.0.0.1:8000>. El ejemplo incluye un monitor deshabilitado; actívalo o crea uno desde el dashboard. `serve` inicia el dashboard y el planificador en un único proceso.

Para sitios que requieren JavaScript o comparación visual, instala Chromium:

```bash
uv run playwright install chromium
```

En una máquina Linux que no tenga las bibliotecas del navegador, un administrador puede instalar Chromium y sus dependencias del sistema con `uv run playwright install --with-deps chromium`. La imagen Docker ya las incluye. El modo visible necesita una sesión gráfica o Xvfb.

La instalación y las pruebas sin navegador utilizan HTTP y mocks; no requieren credenciales ni envían mensajes reales.

## CLI

`--config` y `--verbose` son opciones globales y se escriben antes del subcomando. Si no se especifica `--config`, se utiliza `WEBCHANGESENTINEL_CONFIG` o, en su ausencia, `config.yaml`.

```bash
# Crear una configuración mínima; conserva cualquier archivo existente.
uv run webchangesentinel --config config.yaml init

# Validar YAML y referencias a canales; listar monitores configurados.
uv run webchangesentinel --config config.yaml validate
uv run webchangesentinel --config config.yaml list

# Comprobar un monitor o todos los monitores habilitados una vez.
uv run webchangesentinel --config config.yaml check example
uv run webchangesentinel --config config.yaml check

# Navegador visible para una comprobación de diagnóstico.
uv run webchangesentinel --config config.yaml check example --visible

# Planificador sin dashboard; mantiene el proceso en primer plano.
uv run webchangesentinel --config config.yaml run

# Dashboard y planificador; host local por defecto.
uv run webchangesentinel --config config.yaml serve --host 127.0.0.1 --port 8000

# Diagnóstico con logging detallado.
uv run webchangesentinel --config config.yaml --verbose check example
```

Utiliza `run` o `serve` para una misma base de datos. Ejecutar ambos o iniciar varios workers programa comprobaciones duplicadas.

## Qué significa un cambio

La primera captura correcta establece la base y no genera una alerta. Cada captura posterior se compara con la última captura correcta. Se guardan las capturas exitosas y los cambios detectados, incluidos los cambios que los filtros descartan. Una captura descartada por los filtros también pasa a ser la nueva base. Así, muchos cambios pequeños sucesivos no se acumulan frente a una base antigua.

Un fallo de descarga no sustituye la base. Un fallo de entrega se registra por canal y no impide intentar los demás canales. `check` termina con código distinto de cero si falla una captura o una entrega. El historial y el estado permiten distinguir un cambio detectado de una alerta entregada.

## Configuración y documentación

El archivo [config.example.yaml](config.example.yaml) funciona sin secretos. Para activar un canal, configura sus parámetros y añade su nombre a `channels` del monitor. Las credenciales pueden referenciar variables como `${TELEGRAM_BOT_TOKEN}`; se resuelven al cargar el YAML y no deben guardarse en Git.

- [Configuración completa](docs/configuracion.md): selectores, filtros, programación y canales.
- [Arquitectura](docs/arquitectura.md): componentes, modelo de persistencia y flujo de comprobación.
- [Operación y despliegue](docs/operacion.md): Docker, PostgreSQL, diagnóstico, backups y límites operativos.
- [Smoke test](docs/smoke.md): validación real con una página local y Chromium.

El dashboard carece de autenticación propia. Para acceso remoto, colócalo detrás de un proxy con autenticación y TLS. Los usuarios con acceso pueden configurar URLs y canales; reserva ese acceso a operadores de confianza.

## Docker Compose

```bash
mkdir -p config data
cp config.example.yaml config/config.yaml
export WCS_UID="$(id -u)"
export WCS_GID="$(id -g)"
docker compose build
docker compose run --rm app validate
docker compose up -d app
```

El dashboard queda en <http://127.0.0.1:8000>. `config/` y `data/` son montajes persistentes; la configuración debe ser escribible para que el dashboard pueda guardar cambios. La imagen instala las dependencias fijadas en `uv.lock` y Chromium con sus bibliotecas del sistema.

```bash
docker compose logs -f app
docker compose run --rm app check example
docker compose down
```

La guía de operación describe el perfil PostgreSQL y las variables que se transmiten al contenedor para las alertas.

## Desarrollo y pruebas

```bash
uv sync --extra dev --frozen
uv run pytest -q
```

Las pruebas utilizan bases SQLite temporales y mocks de red/navegador/canales. Cubren extracción, detección, filtros, persistencia, notificaciones y comportamiento de la aplicación sin depender de servicios públicos.

El paquete está en `src/webchangesentinel`; los tests están en `tests`. El archivo `uv.lock` fija las dependencias. Para modificar dependencias, actualiza deliberadamente el manifiesto y el lockfile; para preparar un entorno existente utiliza siempre la instalación congelada.

## Límites

La aplicación utiliza un planificador dentro del proceso, sin cola distribuida. No incluye retención automática: la base y las capturas visuales crecen con las comprobaciones. Las capturas pueden contener datos de las páginas monitoreadas. El despliegue en contenedor no ofrece escritorio gráfico y requiere configuración adicional para depurar con navegador visible o enviar notificaciones desktop.

Las páginas con login, CAPTCHA o protecciones contra automatización pueden requerir configuración o una integración adicional. No se garantiza que una captura pruebe un cambio semántico: contenido dinámico, publicidad, fuentes o animaciones pueden producir diferencias; configura los selectores y filtros para el contenido relevante.
