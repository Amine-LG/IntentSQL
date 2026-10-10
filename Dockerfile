FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INTENTSQL_WORKSPACE=/data

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 intentsql \
    && useradd --system --uid 10001 --gid intentsql --home-dir /app intentsql \
    && mkdir -p /data \
    && chown intentsql:intentsql /data

COPY LICENSE README.md pyproject.toml ./
COPY intentsql ./intentsql
RUN python -m pip install --no-cache-dir --no-deps . && rm -rf build intentsql.egg-info

USER intentsql

VOLUME ["/data"]
EXPOSE 7862

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7862/api/health', timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "intentsql.web:app", "--host", "0.0.0.0", "--port", "7862", "--workers", "1"]
