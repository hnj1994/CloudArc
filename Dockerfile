# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    CLOUDARC_DATA_DIR=/data MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
COPY requirements.lock.txt pyproject.toml README.md ./
RUN pip install -r requirements.lock.txt
COPY cloudarc ./cloudarc
RUN pip install --no-deps . && useradd --system --uid 10001 --home /data cloudarc && mkdir -p /data && chown cloudarc /data
USER cloudarc
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health').status==200 else 1)"
CMD ["cloudarc", "serve", "--host", "0.0.0.0", "--port", "8080"]
