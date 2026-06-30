# Image for the governed stack's Python services (mcpo + the five MCP servers,
# and the gateway). OPA runs from its own official image in compose.
#
# The five servers and their governance libraries are not on PyPI, so they are
# installed from wheels built into docker/wheels by build.sh. Everything else
# resolves from PyPI at build time.
FROM python:3.11-slim

WORKDIR /app

COPY docker/wheels /tmp/wheels
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
        mcpo "mcp>=1.0" fastapi "uvicorn[standard]" httpx \
        opentelemetry-sdk opentelemetry-exporter-otlp-proto-http \
    && rm -rf /tmp/wheels

COPY gateway.py ./
COPY docker/mcpo.docker.json ./mcpo.docker.json
COPY data ./data
COPY policy ./policy

# Seed the demo database and schema catalog into the image, and make logs writable.
RUN mkdir -p /app/logs \
    && python data/sql/build_demo_db.py \
    && python -m schema_scout.cli demo --out data/schema

EXPOSE 8765 8766
