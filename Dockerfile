FROM python:3.12-slim-bookworm

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

RUN pip install --no-cache-dir psycopg2-binary Pillow ExifRead httpx
# ML dependencies for email classifier retraining (optional — retrain will fail gracefully without these)
RUN pip install --no-cache-dir sentence-transformers scikit-learn numpy 2>/dev/null || true
# PostgreSQL client tools (pg_dump, psql) for database backup/restore
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client rsync openssh-client ca-certificates && rm -rf /var/lib/apt/lists/*

# Docker CLI (static client) so the admin Infrastructure tab can inspect sibling
# containers and drive restart buttons via the mounted /var/run/docker.sock.
ARG DOCKER_CLI_VERSION=29.6.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /usr/local/bin docker/docker \
    && chmod +x /usr/local/bin/docker \
    && rm /tmp/docker.tgz

COPY server.py db.py auth.py smtp_client.py ics_calendar.py notes_store.py user_profile_store.py presence_store.py event_log_store.py crm_bot_store.py notification_dispatcher.py telegram_bot.py backup_service.py infra_log.py ./
COPY VERSION ./
COPY CHANGELOG.md ./
COPY init.sql ./
COPY public ./public

RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

ENV PORT=8766
ENV PYTHONUNBUFFERED=1

EXPOSE 8766

VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/api/config', timeout=3)"

CMD ["python3", "server.py"]
