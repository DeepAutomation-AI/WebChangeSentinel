# Configuración

WebChangeSentinel carga un único documento YAML. La CLI acepta `--config PATH`; también puede utilizarse `WEBCHANGESENTINEL_CONFIG`. El dashboard guarda las modificaciones en ese archivo, por lo que su directorio debe ser escribible. Las referencias `${VARIABLE}` permiten separar las credenciales de la configuración; una variable requerida ausente hace fallar la carga.

Las rutas de archivos relativas son relativas al directorio de trabajo del proceso. Ejecuta los comandos desde la raíz del proyecto o utiliza rutas absolutas. La configuración se valida con Pydantic antes de iniciar el servicio.

## Aplicación

| Campo | Ejemplo | Uso |
| --- | --- | --- |
| `database_url` | `sqlite:///data/sentinel.db` | URL SQLAlchemy. Para PostgreSQL: `postgresql+psycopg://...`. |
| `snapshot_dir` | `data/screenshots` | Directorio persistente para capturas visuales. |
| `concurrency` | `4` | Máximo de comprobaciones simultáneas. |
| `notifications` | `{}` | Mapa de canales, con nombres elegidos por el operador. |
| `monitors` | `[]` | Lista de monitores. |

Para SQLite con ruta absoluta, utiliza cuatro barras: `sqlite:////app/data/sentinel.db`. SQLite sirve para un despliegue sencillo de un proceso. PostgreSQL permite separar la base del proceso y requiere el extra `postgres`:

```bash
uv sync --extra dev --extra postgres --frozen
```

## Monitores

```yaml
monitors:
  - id: precio
    name: Precio de producto
    url: https://example.com/producto
    enabled: true
    selector_type: css
    selector: .precio
    engine: http
    interval: 15m
    headless: true
    visual: false
    image_hash: false
    timeout: 30
    retries: 3
    retry_backoff: 1
    user_agents:
      - Mozilla/5.0 (compatible; WebChangeSentinel/1.0)
    proxy: null
    filters:
      threshold_percent: 2
      min_changed_chars: 3
      keywords: []
      ignore_selectors: [.fecha-actualizacion, .publicidad]
      ignore_patterns: ['Actualizado a las \d{2}:\d{2}']
      ignore_case: false
    channels: []
```

| Campo | Descripción |
| --- | --- |
| `id` | Identificador único, estable y apto para la CLI/URL. |
| `name` | Nombre visible. |
| `url` | URL HTTP o HTTPS a comprobar. |
| `enabled` | Activa las comprobaciones programadas. |
| `selector_type` | `css`, `xpath` o `full`; el predeterminado es `full`. |
| `selector` | Selector requerido para CSS/XPath; omítelo o usa `null` con `full`. |
| `engine` | `http` o `playwright`. |
| `interval` | Frecuencia: `30s`, `5m`, `1h`, `1d`. |
| `headless` | `false` abre un navegador visible cuando se utiliza Playwright. |
| `visual` | Guarda y compara capturas de pantalla; requiere Chromium. |
| `image_hash` | Activa la comparación perceptual de las capturas de pantalla. |
| `timeout` | Tiempo límite en segundos de una operación de captura. |
| `retries` | Número de intentos adicionales tras el primero. |
| `retry_backoff` | Base en segundos para el backoff exponencial. |
| `user_agents` | Lista de user agents disponibles para las solicitudes. |
| `proxy` | URL de proxy o `null`. |
| `filters` | Reglas de normalización y selección de cambios. |
| `channels` | Nombres de canales declarados en `notifications`. |

### CSS, XPath y texto completo

Usa CSS cuando el contenido relevante tenga un selector estable, por ejemplo `.precio`, `#stock` o `article`. XPath resulta útil para seleccionar por estructura o atributos, como `//main//h1` o `//*[@data-testid="price"]`. Con `selector_type: full`, se compara el texto de la página después de limpiar el HTML.

Los selectores que no encuentran contenido provocan un fallo de captura; no se consideran una desaparición válida ni sustituyen el snapshot anterior. Ajusta el selector usando las herramientas del navegador y prueba con `check ID`.

Si cambias la URL, selector, tipo de selector, motor, modo visual, `image_hash` o los selectores ignorados, el servicio establece una base nueva para evitar comparar extracciones incompatibles. Las capturas anteriores siguen en el historial.

### Renderizado JavaScript y capturas visuales

Configura `engine: playwright` si el HTML recibido por HTTP no contiene el contenido que ves en el navegador. Chromium carga la página y se extrae el DOM renderizado. `visual: true` o `image_hash: true` también necesitan Playwright aunque el campo `engine` sea `http`.

La navegación espera `domcontentloaded`; para CSS espera que exista el selector. También intenta una espera breve de red inactiva. No garantiza que todo widget que se actualiza mucho después de la carga haya terminado: utiliza un selector estable y comprueba la extracción de tu sitio.

Las capturas visuales abarcan toda la página, con viewport inicial de 1280×800 y animaciones desactivadas durante el screenshot. Ambos modos visuales comparan el hash perceptual de la imagen. No es una imagen de diferencias píxel a píxel ni un diff recortado al selector. El screenshot no aplica los selectores ni los patrones ignorados del texto, por lo que otras áreas de la página pueden disparar una diferencia visual.

Para depurar:

```yaml
engine: playwright
headless: false
timeout: 60
```

El modo visible necesita un display disponible. En un Linux sin escritorio, Xvfb permite un display virtual, por ejemplo `xvfb-run -a uv run webchangesentinel --config config.yaml check precio`. Esto no muestra una ventana en el equipo del operador; proporciona el entorno gráfico que Chromium requiere.

También puedes utilizar `check precio --visible` para forzar Playwright visible durante una sola comprobación, sin modificar el YAML.

### Frecuencia, concurrencia y reintentos

Cada monitor conserva su propia frecuencia. El planificador evita ejecutar dos instancias simultáneas del mismo job dentro de un proceso y limita el trabajo mediante `concurrency`. Usa un solo proceso del servicio por configuración/base de datos.

Con `retries: 3` puede haber hasta cuatro intentos. El backoff aumenta de forma exponencial a partir de `retry_backoff`. Un fallo definitivo queda reflejado en el estado; se conserva la última captura correcta para la siguiente comparación. Los reintentos son para la captura: no constituyen una cola de mensajes de alertas.

La rotación de user agents y el proxy son parámetros de transporte. No proporcionan autenticación a las páginas ni garantizan acceso a contenido protegido. Si el proxy requiere credenciales, puedes referenciar su URL mediante `${MONITOR_PROXY}`; no incluyas credenciales literales en el YAML.

## Filtros y cálculo de diferencias

| Campo | Efecto |
| --- | --- |
| `threshold_percent` | Umbral mínimo de diferencia porcentual para avisar. |
| `min_changed_chars` | Mínimo de caracteres modificados para cambios textuales. |
| `keywords` | Palabras o frases que deben aparecer en el texto modificado; lista vacía desactiva esta condición. |
| `ignore_selectors` | Selectores CSS de nodos que deben excluirse durante la extracción textual. |
| `ignore_patterns` | Expresiones regulares Python aplicadas secuencialmente con `MULTILINE` para eliminar texto variable. |
| `ignore_case` | Normaliza las mayúsculas/minúsculas antes de comparar, después de los patrones. |

La diferencia textual es `100 × (1 − difflib.SequenceMatcher.ratio())`. La diferencia visual es el porcentaje de bits distintos entre los hashes perceptuales. El umbral se evalúa con el mayor porcentaje entre ambos. `min_changed_chars` no bloquea un cambio visual; `keywords` sí requiere coincidencias en texto modificado y puede descartar un cambio exclusivamente visual.

Empieza con selectores estrechos y añade los filtros después de observar capturas reales. Una expresión regular demasiado amplia puede eliminar el contenido que quieres monitorear. En YAML, las comillas simples facilitan escribir patrones con barras inversas.

```yaml
filters:
  threshold_percent: 1
  min_changed_chars: 2
  keywords: [disponible, agotado]
  ignore_selectors: [script, style, .reloj]
  ignore_patterns: ['\b\d{2}:\d{2}:\d{2}\b']
  ignore_case: true
```

Cada captura correcta se convierte en la base siguiente, aunque no genere alerta. El porcentaje sirve para comparar dos capturas consecutivas y no representa el porcentaje del producto, precio o disponibilidad que haya cambiado.

## Alertas

Los nombres de canales son libres. Un monitor puede utilizar varios:

```yaml
notifications:
  equipo:
    kind: telegram
    bot_token: "${TELEGRAM_BOT_TOKEN}"
    chat_id: "${TELEGRAM_CHAT_ID}"
  incidentes:
    kind: discord
    webhook_url: "${DISCORD_WEBHOOK_URL}"
monitors:
  - id: ejemplo
    name: Ejemplo
    url: https://example.com
    interval: 1h
    selector_type: full
    channels: [equipo, incidentes]
```

La entrega a un canal fallido no interrumpe los demás. Los resultados de entrega se guardan con el evento. La primera captura establece la base y no prueba la entrega: para probar un canal utiliza una página controlada y produce un cambio que supere los filtros.

### Email SMTP

```yaml
notifications:
  correo:
    kind: email
    host: "${SMTP_HOST}"
    port: 587
    username: "${SMTP_USERNAME}"
    password: "${SMTP_PASSWORD}"
    from_address: "${SMTP_FROM}"
    to_addresses: ["${SMTP_TO}"]
    starttls: true
    ssl: false
```

Utiliza la contraseña o credencial de aplicación que requiera tu proveedor. Con TLS implícito, usa normalmente `port: 465`, `ssl: true` y `starttls: false`; con STARTTLS, normalmente `port: 587`, `ssl: false` y `starttls: true`. La configuración de SMTP depende del proveedor. No desactives la validación TLS para resolver un problema de certificados.

### Telegram

`bot_token` es el token emitido para tu bot; `chat_id` identifica el chat o canal autorizado. Inicia la conversación con el bot o añádelo al grupo/canal y concede los permisos necesarios. Configura ambos valores en el entorno, no en mensajes de chat ni archivos versionados.

### Discord y Slack

```yaml
notifications:
  discord:
    kind: discord
    webhook_url: "${DISCORD_WEBHOOK_URL}"
  slack:
    kind: slack
    webhook_url: "${SLACK_WEBHOOK_URL}"
```

Cada URL de webhook es una credencial. Crea el webhook en el canal de destino y guarda su URL como variable de entorno. Los mensajes de Discord no activan menciones. Los webhooks se invocan sin seguir redirecciones.

### Escritorio

```yaml
notifications:
  local:
    kind: desktop
```

Este canal necesita una sesión de escritorio. En Linux requiere `DISPLAY` o `WAYLAND_DISPLAY` y un backend disponible, como `notify-send` o `plyer`. Un servidor o el contenedor predeterminado no dispone de ese escritorio; utiliza allí otro canal.

## Variables y cambios desde el dashboard

La configuración conserva las referencias `${VARIABLE}` cuando se guarda desde el dashboard. Las variables deben existir en el entorno del proceso que arranca la aplicación. Definirlas en otra sesión de shell no modifica un proceso ya iniciado: vuelve a iniciar el servicio después de cambiarlas.

El dashboard permite administrar monitores, pero los canales y sus secretos se configuran mediante el YAML y el entorno. Tras editar el YAML externamente, valida la configuración y reinicia el servicio para aplicar sus cambios.
