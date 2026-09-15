# Multi-stage: build tools stay out of the runtime image.
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/auditor/.local/bin:${PATH}"

# Non-root. The original image ran everything as root.
RUN useradd --create-home --uid 10001 auditor

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chown=auditor:auditor /root/.local /home/auditor/.local

WORKDIR /app
# .dockerignore excludes .env and key files. Verify with:
#   docker run --rm --entrypoint sh IMAGE -c 'ls -a /app | grep -i -E "env|key"'
COPY --chown=auditor:auditor . .

RUN mkdir -p /app/data && chown auditor:auditor /app/data
USER auditor

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "app.py", \
            "--server.port=8501", "--server.address=0.0.0.0", \
            "--server.headless=true", "--browser.gatherUsageStats=false"]
