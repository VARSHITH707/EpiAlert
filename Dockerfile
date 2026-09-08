# EpiAlert — disease outbreak early warning
#
# Build:  docker build -t epialert .
# Run:    docker compose up
#
# The image carries the application only. The database and dataset are mounted
# as volumes, so rebuilding the image never touches your data.

FROM python:3.11-slim

# Fail fast and log immediately rather than buffering output until exit.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilds when
# requirements.txt changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY run_all.py README.md ARCHITECTURE.md ./

# Run as a non-root user. A container process that does not need root should
# not have it.
RUN useradd --create-home --shell /bin/bash epialert \
    && chown -R epialert:epialert /app
USER epialert

EXPOSE 8000

# Fails the healthcheck if the app stops answering, so an orchestrator can
# restart it rather than leaving a dead container in place.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "src.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
