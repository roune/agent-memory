# agent-memory-server: the write API and MCP (/mcp) next to Qdrant.
# docs/server-plan.md, "Packaging". compose.yaml builds it as the `server` service.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/agent-memory/venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /opt/agent-memory/src
# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra server --no-install-project
COPY README.md LICENSE NOTICE ./
COPY scripts ./scripts
RUN uv sync --frozen --extra server --no-editable

ENV PATH=/opt/agent-memory/venv/bin:$PATH

# The pinned checkpoint, baked in outside /data so the volume can't hide it.
# The server never downloads a model at runtime (HF_HUB_OFFLINE below).
RUN python -c "from embedder import FastEmbedBge; FastEmbedBge('/opt/agent-memory/models').ensure_checkpoint()" \
 && rm -rf /opt/agent-memory/src \
 && chmod -R a+rX /opt/agent-memory \
 && mkdir /data && chown 1000:1000 /data

ENV AGENT_MEMORY_SERVER_DATA=/data \
    AGENT_MEMORY_MODELS_DIR=/opt/agent-memory/models \
    HF_HUB_OFFLINE=1 \
    HOME=/tmp \
    PYTHONUNBUFFERED=1

# compose.yaml runs it as the host user (AGENT_MEMORY_UID/GID), so files in
# data/server stay yours. 1000:1000 is only the default.
USER 1000:1000
WORKDIR /data
EXPOSE 8765

# python:3.12-slim has no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/v1/health', timeout=4)"]

CMD ["agent-memory-server", "serve"]
