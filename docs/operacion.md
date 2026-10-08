# Operación y despliegue

## Preparar un entorno local o en la nube

```bash
uv sync --extra dev --frozen
cp config.example.yaml config.yaml
uv run webchangesentinel --config config.yaml validate
uv run pytest -q
```

Instala Chromium únicamente si hay monitores JavaScript o visuales:

```bash
uv run playwright install chromium
```

Para Linux sin las bibliotecas del navegador, instala las dependencias del sistema mediante el comando oficial `playwright install --with-deps chromium` con los permisos administrativos necesarios. No omitas la validación de certificados, firmas o checksums al instalar dependencias. El motor HTTP no necesita un navegador.

Configura las variables requeridas por tus canales a través del gestor de entorno o secretos de tu plataforma. Utiliza una ruta de datos persistente y escribible. Comprueba la configuración antes de arrancar y ejecuta una comprobación manual antes de dejar el servicio programado.

```bash
uv run webchangesentinel --config config.yaml check ID
uv run webchangesentinel --config config.yaml serve
```

## Prueba con una página controlada

Este ejemplo permite verificar el cambio textual sin enviar alertas externas. Inicia un servidor local en otra terminal:

```bash
mkdir -p /tmp/wcs-demo
printf '<html><body><main>Precio: 10</main></body></html>\n' > /tmp/wcs-demo/index.html
python -m http.server 8765 --bind 127.0.0.1 --directory /tmp/wcs-demo
```

Guarda este YAML en `demo.yaml` o añade el monitor mediante el dashboard:

```yaml
database_url: sqlite:///data/demo.db
snapshot_dir: data/demo-screenshots
notifications: {}
monitors:
  - id: demo
    name: Prueba local
    url: http://127.0.0.1:8765/
    selector_type: css
    selector: main
    interval: 1m
    channels: []
    filters:
      threshold_percent: 0
      min_changed_chars: 1
```

```bash
uv run webchangesentinel --config demo.yaml check demo
printf '<html><body><main>Precio: 12</main></body></html>\n' > /tmp/wcs-demo/index.html
uv run webchangesentinel --config demo.yaml check demo
uv run webchangesentinel --config demo.yaml serve
```

La primera comprobación crea la base. La segunda registra un cambio. Para comprobar JavaScript, utiliza una página de prueba que modifique el DOM y configura `engine: playwright` después de instalar Chromium. Para comprobar alertas, añade un canal de prueba y produce otro cambio; no confundas una captura base con una prueba de entrega.

La [guía de smoke test](smoke.md) describe la validación automática de extremo a extremo contra servidores locales, incluido Playwright real.

## Docker Compose

Necesitas Docker y Docker Compose v2. Prepara los directorios antes de montar el contenedor para que no los cree Docker con un propietario diferente:

```bash
mkdir -p config data
cp config.example.yaml config/config.yaml
export WCS_UID="$(id -u)"
export WCS_GID="$(id -g)"
docker compose build
docker compose run --rm app validate
docker compose up -d app
```

La imagen utiliza Python 3.12, instala dependencias con `uv sync --frozen --no-dev --extra postgres`, e instala Chromium con sus dependencias del sistema. El proceso se ejecuta como usuario `sentinel`; Compose utiliza el UID/GID del operador para poder escribir los montajes de configuración y datos. Conserva `WCS_UID` y `WCS_GID` al recrear los contenedores, especialmente si tu usuario no tiene UID/GID 1000.

| Montaje | Uso |
| --- | --- |
| `./config:/app/config` | YAML persistente y modificable desde el dashboard. |
| `./data:/app/data` | SQLite y screenshots persistentes. |

El puerto se publica únicamente en loopback: `127.0.0.1:8000`. Puedes cambiar el puerto local con `WCS_PORT`. Para acceso remoto configura un proxy autenticado y TLS, y adapta el enlace de red de acuerdo con ese proxy. Mantén un solo contenedor `app`.

```bash
docker compose logs -f app
docker compose run --rm app validate
docker compose run --rm app list
docker compose run --rm app check ID
docker compose restart app
docker compose down
```

Una comprobación manual mientras el servicio programa el mismo monitor puede solaparse, porque es otro proceso. Para pruebas controladas, detén `app`, ejecuta el comando y vuelve a iniciarlo. `docker compose down` conserva los directorios bind y el volumen PostgreSQL; `down -v` elimina los volúmenes nombrados.

### Construcción detrás de un proxy

Docker BuildKit puede necesitar la configuración de red del entorno durante la descarga de dependencias. Los argumentos especiales de proxy se heredan por nombre; no incluyas sus valores en comandos guardados ni en el Dockerfile:

```bash
docker build --network=host \
  --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
  -t webchangesentinel:dev .
```

Si la plataforma utiliza una CA propia para su proxy TLS, proporciona únicamente el certificado público autorizado mediante el secreto opcional de BuildKit `platform_ca`:

```bash
docker build --network=host \
  --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
  --secret id=platform_ca,src=/ruta/al/certificado-publico.crt \
  -t webchangesentinel:dev .
```

La imagen incorpora ese certificado al almacén de confianza del sistema. uv utiliza ese almacén y el descargador de Playwright recibe la misma cadena de confianza mediante `NODE_EXTRA_CA_CERTS`. Las verificaciones TLS, firmas de paquetes y hashes del lockfile permanecen activas. El certificado público puede quedar en la imagen; los valores de proxy y las credenciales no se copian al Dockerfile.

Si el hostname del proxy se resuelve en el entorno mediante `/etc/hosts`, BuildKit puede no heredar esa entrada. Añade `--add-host HOSTNAME:IP` con la entrada verificada del entorno. No cambies el hostname TLS ni desactives la validación del certificado. Si utilizas Compose, configura el mismo certificado como secreto de construcción en un archivo de override; el montaje debe llamarse `platform_ca`.

### Variables de alertas en Compose

Compose transmite `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL` y `SLACK_WEBHOOK_URL` al contenedor. Define las que necesites antes de iniciar y habilita los bloques correspondientes en `config/config.yaml`.

También transmite `MONITOR_PROXY` para configurar un proxy por referencia. Si utilizas otros nombres de variables en el YAML, añádelos al bloque `environment` de Compose o a un archivo de override; una variable del host no se transmite automáticamente al contenedor.

Puedes utilizar el gestor de secretos de la plataforma o un archivo `.env` local con permisos restrictivos, excluido de Git y del contexto Docker. Los secretos no deben introducirse en el Dockerfile ni en argumentos de construcción. Después de cambiar variables, recrea `app` con `docker compose up -d --force-recreate app`; reiniciar el mismo contenedor no cambia su entorno.

### PostgreSQL opcional

El servicio `postgres` se activa con el perfil `postgres`. Antes de iniciarlo, define `POSTGRES_PASSWORD` y `DATABASE_URL` mediante tu entorno o gestor de secretos. Por defecto la base se llama `webchangesentinel` y el usuario `sentinel`; puedes cambiar `POSTGRES_DB` y `POSTGRES_USER`.

`DATABASE_URL` debe tener esta estructura, con la contraseña escapada como componente de URL:

```text
postgresql+psycopg://sentinel:<contraseña-escapada>@postgres:5432/webchangesentinel
```

No escribas la contraseña literal en el YAML. Sustituye `database_url` en `config/config.yaml` por:

```yaml
database_url: "${DATABASE_URL}"
```

```bash
docker compose --profile postgres up -d postgres
docker compose --profile postgres ps
# Espera a que PostgreSQL indique healthy antes de iniciar la aplicación.
docker compose --profile postgres run --rm app validate
docker compose --profile postgres up -d app
```

La base usa el volumen nombrado `postgres_data`; no publica un puerto en el host. El contenedor de PostgreSQL requiere una contraseña no vacía para inicializar una base nueva. Cambiar `POSTGRES_PASSWORD` en Compose después de inicializar el volumen no cambia por sí solo la contraseña almacenada en PostgreSQL.

El cambio de `database_url` crea/usa la base seleccionada; no migra automáticamente los datos existentes desde SQLite. Conserva un backup y planifica una migración de datos por separado si necesitas mantener ese historial.

## Logging, estado y errores

El proceso escribe logs en su salida estándar. Usa `--verbose` antes del subcomando para más detalle. El dashboard muestra el estado, las últimas comprobaciones y el historial. La CLI `check` devuelve un código de error si no puede capturar un monitor o entregar una alerta.

| Problema | Diagnóstico y acción |
| --- | --- |
| Variable requerida ausente | Define la variable en el entorno del proceso y vuelve a validar. |
| Canal desconocido | Comprueba que cada nombre de `channels` exista en `notifications`. |
| Selector sin coincidencias | Verifica CSS/XPath y compara el HTML recibido con el DOM del navegador. |
| Contenido JavaScript ausente | Cambia a `engine: playwright` e instala Chromium. |
| Ejecutable de Chromium ausente | Ejecuta `uv run playwright install chromium` en el mismo entorno Python. |
| Bibliotecas Linux ausentes | Instala las dependencias oficiales de Playwright o utiliza la imagen Docker. |
| Navegador visible sin display | Utiliza escritorio/Xvfb o activa `headless`. |
| Timeout o conexión rechazada | Revisa URL, conectividad, proxy y `timeout`; prueba HTTP y navegador por separado. |
| HTTP 403/429 | Revisa acceso y frecuencia del sitio; el backoff no concede permisos de acceso. |
| Error TLS | Comprueba reloj, CA y configuración del proxy/proveedor sin desactivar la verificación. |
| SQLite o YAML no escribible | Revisa propietario/permisos del directorio y UID/GID de Compose. |
| SMTP falla | Comprueba host/puerto, STARTTLS frente a TLS implícito y credencial del proveedor. |
| Telegram falla | Comprueba token, chat autorizado y permisos del bot. |
| Desktop falla | Comprueba sesión gráfica y backend local; usa otro canal en servidores. |
| No llega alerta tras primer check | La primera captura establece la base; produce un cambio posterior. |
| Cambios visibles no avisan | Revisa selectors, porcentaje, caracteres mínimos, keywords y resultado del evento. |
| Alertas repetidas | Comprueba que no haya varios procesos/workers o contenido dinámico sin filtrar. |

No pegues URLs de webhooks, tokens, credenciales ni capturas sensibles en reportes públicos. Para reportar un fallo, incluye versiones, motor, mensaje de error sanitizado y una página de prueba que reproduzca el problema.

## Backups y crecimiento

Se guardan todas las capturas exitosas; no hay retención automática. El crecimiento depende de la frecuencia, tamaño del HTML y capturas visuales. Supervisa el tamaño de la base y `snapshot_dir`, y reserva disco para Chromium y snapshots.

Para un backup SQLite sencillo y consistente:

1. Detén el proceso `app` o la CLI que esté utilizando la base.
2. Copia el directorio `data/`, incluido el archivo SQLite y cualquier archivo auxiliar, y el YAML de `config/` a almacenamiento de backup.
3. Guarda las variables secretas mediante el gestor de secretos, separado del backup de archivos.
4. Inicia el proceso y comprueba un monitor.

Para PostgreSQL utiliza `pg_dump` y conserva también `snapshot_dir` y la configuración. Ensaya la restauración antes de depender del backup. No elimines screenshots o filas manualmente sin conocer sus referencias: podrías dejar eventos sin sus capturas. El archivo de configuración y los datos del servicio deben recuperarse juntos.

## Despliegue de servicio

Utiliza Compose o un supervisor de procesos para reiniciar el servicio ante fallos. El comando de servicio es `webchangesentinel --config /ruta/config.yaml run` o `serve`, con un usuario sin privilegios, un directorio de trabajo explícito y rutas persistentes. La frecuencia debe dejar tiempo suficiente para la captura y sus posibles reintentos.

Para monitorización de mayor volumen, distribuye conjuntos de monitores en despliegues independientes con bases/configuraciones separadas. La versión actual no incluye coordinación de jobs entre réplicas, autenticación, cola de entrega duradera ni migraciones de esquema versionadas.
