# Shopfront MCP server image (ARM64: AgentCore Runtime requires it; Fargate Graviton is cheaper too).
#
# Build from the REPOSITORY ROOT (the server needs ../packages/mcp-tasks, ../schema and ../profiles):
#   docker build --platform linux/arm64 -f server/Dockerfile -t shopfront-mcp .
# Run locally (dev secrets, in-memory storage). Publish on loopback only: the DEV-ONLY keys are public,
# and the image binds 0.0.0.0 inside the container, so `-p 8000:8000` would expose them to the LAN:
#   docker run --rm -p 127.0.0.1:8000:8000 -e SHOPFRONT_ENV=dev -e SHOPFRONT_STORAGE=memory shopfront-mcp
#
# The image defaults to SHOPFRONT_ENV=prod + DynamoDB, so it refuses to start (exit code 2, one line per
# problem on stderr) until real secrets are injected (SHOPFRONT_REQUEST_STATE_KEYS,
# SHOPFRONT_OWNER_LINK_KEYS, SHOPFRONT_ID_KEY: Secrets Manager) and the public names are set
# (SHOPFRONT_ALLOWED_HOSTS=mcp.example.com, SHOPFRONT_PUBLIC_BASE_URL=https://mcp.example.com).

FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:0.10.5 AS uv

FROM --platform=linux/arm64 python:3.12-slim

COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/server/.venv

WORKDIR /app/server

# 1) Dependencies only (cached until the lock file or the tasks package changes).
COPY packages/mcp-tasks /app/packages/mcp-tasks
COPY server/pyproject.toml server/uv.lock server/README.md /app/server/
RUN uv sync --frozen --no-dev --no-install-project

# 2) The server itself (installed editable, so config.REPO_ROOT resolves to /app).
COPY server/src /app/server/src
RUN uv sync --frozen --no-dev

# 3) Data the server reads at startup: the profile JSON Schema and the gold profiles (memory storage).
COPY schema /app/schema
COPY profiles /app/profiles

# Unprivileged runtime user; the code and venv stay root-owned and read-only to it.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin shopfront
USER shopfront

ENV PATH="/app/server/.venv/bin:${PATH}" \
    SHOPFRONT_ENV=prod \
    SHOPFRONT_STORAGE=dynamodb \
    SHOPFRONT_HOST=0.0.0.0 \
    SHOPFRONT_PORT=8000 \
    SHOPFRONT_PROFILE_SCHEMA_PATH=/app/schema/business-profile.schema.json \
    SHOPFRONT_PROFILE_DIRS=/app/profiles \
    SHOPFRONT_FALLBACK_PROFILE=/app/schema/example-profile.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

# AgentCore Runtime contract: 0.0.0.0:8000, MCP at POST /mcp.
CMD ["python", "-m", "shopfront_server"]
