FROM python:3.12-slim

WORKDIR /app

# System deps for psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user
RUN useradd -m anchor && chown -R anchor:anchor /app
USER anchor

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')"

CMD ["uvicorn", "agents.watcher:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
