# Container image for the Saxon AI Context Engine API, built for Azure
# Container Apps (see docs/internal/infrastructure-plan.md). Only the API is
# containerized -- Neo4j is a separate managed service (AuraDB or self-hosted),
# not something this image runs.
FROM python:3.11-slim

WORKDIR /app

# System deps for the neo4j/graphiti-core packages' native extensions.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code, ontology, and frontend/ only -- data/, scripts/, tests/, docs/ aren't
# needed to run the API and would just bloat the image (see .dockerignore).
# frontend/ is required, not optional: app/main.py mounts it as static files
# and serves it at /ui -- without it the app fails to even start.
COPY app/ ./app/
COPY ontology/ ./ontology/
COPY frontend/ ./frontend/

# Config is supplied at deploy time via Container Apps environment variables
# (per docs/internal/infrastructure-plan.md's "platform env vars" approach),
# not baked into the image -- no .env or config/tenants.json is copied in.

EXPOSE 8000

# --proxy-headers: Container Apps sits behind its own ingress/reverse proxy,
# so the app needs to trust the X-Forwarded-* headers it sets rather than
# seeing every request as if it came from localhost.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
