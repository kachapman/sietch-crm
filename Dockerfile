FROM python:3.12-slim-bookworm

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

# ML dependencies for email classifier retraining (optional — retrain will fail gracefully without these)
RUN pip install --no-cache-dir sentence-transformers scikit-learn numpy 2>/dev/null || true
RUN pip install --no-cache-dir psycopg2-binary Pillow ExifRead httpx imap_tools
# Base tools (curl is required by the docker-cli install step below; otherwise
# `python:3.12-slim-bookworm` ships without it and the build fails).
# PostgreSQL client tools (pg_dump, psql) for database backup/restore.
# ca-certificates so HTTPS fetches (curl, backup remote) work.
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client rsync openssh-client ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI (static client) so the admin Infrastructure tab can inspect sibling
# containers and drive restart buttons via the mounted /var/run/docker.sock.
#
# BUG (fixed): the archive member is `docker/docker` -- a file INSIDE a `docker/`
# directory. `tar -xzf ... -C /usr/local/bin docker/docker` therefore creates a
# DIRECTORY at /usr/local/bin/docker with the client inside it, and tar does not
# flatten. `shutil.which("docker")` then resolves to a directory rather than an
# executable, so every `docker` call failed with "Permission denied" even as root
# and the admin Infrastructure tab reported the socket as unmounted.
# Extract to a temp dir and `install` the binary flat onto PATH instead.
ARG DOCKER_CLI_VERSION=29.6.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp docker/docker \
    && install -m 0755 /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker.tgz /tmp/docker

# All top-level Python modules server.py may import (and the test-server).
# Keeping this as a glob avoids the "missing module → container crash" failure
# mode called out in AGENTS.md when a new .py file is added at the repo root.
COPY *.py ./
COPY VERSION ./
COPY CHANGELOG.md ./
COPY init.sql ./
# Migrations directory — db.init_db() applies these on first boot against
# a fresh DB to catch up with the schema additions documented in
# migrations/0??_*.sql (init.sql alone is no longer enough).
COPY migrations ./migrations
COPY scanner ./scanner
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
