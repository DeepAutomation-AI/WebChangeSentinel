# Validación real sin servicios externos

La suite `pytest` cubre las reglas de negocio y los adaptadores mediante mocks. El smoke complementa esas pruebas con HTTP real, Chromium real, SQLite real y el dashboard servido por Uvicorn. Todas las URLs utilizadas pertenecen a servidores temporales en `127.0.0.1`; no envía alertas ni necesita acceso a Internet durante la ejecución.

Desde la raíz del repositorio, con las dependencias y Chromium instalados:

```bash
.venv/bin/python scripts/smoke.py
```

Si Playwright utiliza una ubicación personalizada para sus navegadores, conserva la misma variable usada al instalarlos. En el entorno cloud preparado:

```bash
PLAYWRIGHT_BROWSERS_PATH=/workspace/.cache/ms-playwright .venv/bin/python scripts/smoke.py
```

Para una instalación local inicial de Chromium:

```bash
.venv/bin/python -m playwright install chromium
```

El script comprueba:

- Captura HTTP con selector CSS, XPath y texto completo.
- Captura Playwright de contenido modificado por JavaScript después de cargar la página.
- Estados `baseline`, `unchanged` y `changed`, incluyendo un cambio exclusivamente visual.
- HTML limpio, hashes SHA-256, perceptual hashes, PNG válidos, snapshots y eventos en SQLite.
- Arranque del scheduler junto al dashboard y cierre de ambos al terminar.
- `/health`, dashboard HTML, comprobación manual, historial y CRUD mediante la API REST real.
- Dashboard renderizado en Chromium, carga local de HTMX, screenshots del historial y ausencia de desbordamiento horizontal en móvil.

El resultado termina con JSON que incluye `"result": "passed"`. Una comprobación fallida conserva el error y devuelve un código de salida distinto de cero. Los servidores, la base de datos, el YAML y las capturas se eliminan al finalizar, incluso ante fallos.

Para conservar únicamente las capturas del dashboard y revisar su presentación:

```bash
PLAYWRIGHT_BROWSERS_PATH=/workspace/.cache/ms-playwright \
  .venv/bin/python scripts/smoke.py --artifacts /tmp/webchangesentinel-smoke
```

Se generan `dashboard-desktop.png` y `dashboard-mobile.png` en el directorio indicado. Esta comprobación no sustituye la configuración de credenciales SMTP, Telegram, Discord o Slack ni demuestra la entrega a esos proveedores.
