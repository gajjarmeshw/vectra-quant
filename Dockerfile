# SENTINEL backend. ARM (Graviton t4g) and amd64 both build from this.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl ca-certificates \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY config/ ./config/
COPY docs/ ./docs/

# growwapi caches its instrument master into its own package directory, so that
# directory must be writable by the unprivileged runtime user.
RUN useradd -m -u 10001 sentinel \
 && mkdir -p /app/data \
 && chown -R sentinel /app \
 && chown -R sentinel "$(python -c 'import growwapi,os;print(os.path.dirname(growwapi.__file__))')"
USER sentinel

ENV PYTHONPATH=/app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# One worker on purpose: the RiskEngine is a single in-process instance and the
# scheduler must not run twice.
CMD ["uvicorn", "sentinel.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
