FROM python:3.12-slim

LABEL org.opencontainers.image.title="AI Bridge Watchdog" \
      org.opencontainers.image.description="CLI utility for monitoring AI Worker Bridge async tasks" \
      org.opencontainers.image.base.name="python:3.12-slim"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 1000 ai-watchdog \
    && useradd --uid 1000 --gid ai-watchdog --create-home --shell /usr/sbin/nologin ai-watchdog

COPY --chown=ai-watchdog:ai-watchdog watchdog.py /app/watchdog.py

USER ai-watchdog

ENTRYPOINT ["python", "/app/watchdog.py"]
