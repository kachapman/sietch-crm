FROM python:3.12-slim-bookworm

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

# mail_oauth.py holds the shared OAuth token refresh (used by
# mail_scanner.py and by server.py) — it must be in the image or the
# scanner fails to import.
COPY mail_scanner.py scanner_service.py train_ml_head.py db.py event_bus.py user_profile_store.py oauth_providers.py mail_oauth.py crm_bot_store.py infra_log.py ./
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt

ARG INSTALL_ML=0
RUN if [ "$INSTALL_ML" = "1" ]; then \
      pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
      pip install --no-cache-dir sentence-transformers scikit-learn numpy && \
      echo "ML dependencies installed (CPU-only torch)"; \
    else \
      echo "ML dependencies skipped (INSTALL_ML=0)"; \
    fi

RUN mkdir -p /app/data/mail_scanner && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1
ENV SCANNER_PORT=8787

EXPOSE 8787

VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request, os; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('SCANNER_PORT','8787') + '/status', timeout=3)"

CMD ["python3", "scanner_service.py"]