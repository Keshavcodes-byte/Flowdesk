FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOMATION_DB_PATH=/app/data/automation.db \
    AUTOMATION_OUTPUT_DIR=/app/data/outputs \
    AUTOMATION_QUEUE_DIR=/app/data/queue

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir . \
    && mkdir -p /app/data/outputs /app/data/queue \
    && useradd --create-home --uid 10001 flowdesk \
    && chown -R flowdesk:flowdesk /app

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "64", "--timeout-keep-alive", "5"]
