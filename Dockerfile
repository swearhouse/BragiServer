FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRAGI_DATA_DIR=/var/lib/bragi-server

WORKDIR /app

COPY pyproject.toml Readme.md ./
COPY src/ ./src/

RUN python -m pip install --no-cache-dir . \
    && useradd --system --no-create-home --shell /usr/sbin/nologin bragi \
    && mkdir -p "$BRAGI_DATA_DIR" \
    && chown bragi:bragi "$BRAGI_DATA_DIR"

USER bragi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["python", "-m", "bragi_server"]
