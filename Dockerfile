FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM python:3.12-slim

COPY --from=uv /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NATIVE_TLS=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    WEBCHANGESENTINEL_CONFIG=/app/config/config.yaml \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=secret,id=platform_ca,required=false \
    if [ -s /run/secrets/platform_ca ]; then \
        cp /run/secrets/platform_ca /usr/local/share/ca-certificates/wcs-platform.crt; \
        update-ca-certificates; \
    fi \
    && uv sync --frozen --no-dev --extra postgres --no-editable \
    && playwright install --with-deps chromium \
    && rm -rf /tmp/uv-cache /var/lib/apt/lists/*

COPY config.example.yaml /app/config/config.yaml
RUN groupadd --gid 10001 sentinel \
    && useradd --uid 10001 --gid sentinel --create-home --home-dir /app/sentinel-home sentinel \
    && mkdir -p /app/data /app/config \
    && chown -R sentinel:sentinel /app/data /app/config

USER sentinel
EXPOSE 8000
ENTRYPOINT ["webchangesentinel"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
