FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AI_BRIDGE_CONFIG=/app/configs/config.yaml \
    AI_BRIDGE_DATA_DIR=/app/data \
    AI_BRIDGE_LOG_DIR=/app/logs

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /app/configs /app/data /app/logs \
    && chown -R app:app /app

COPY pyproject.toml README.md /app/
COPY ai_bridge /app/ai_bridge

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir '.[test]' \
    && chown -R app:app /app \
    && find /app -type d -exec chmod 755 {} + \
    && find /app -type f -exec chmod 644 {} +

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json, urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3); raise SystemExit(0 if json.load(r).get('ok') is True else 1)"

CMD ["uvicorn", "ai_bridge.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
