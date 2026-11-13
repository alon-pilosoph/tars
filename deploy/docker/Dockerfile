# Chat-only TARS ([audio] enabled = false, docs/deployment.md#without-a-microphone): Telegram and the web UI, without
# the voice's packages (the optional extra "voice") or PortAudio. Built from the repository's root:
#
#   docker build -f deploy/docker/Dockerfile -t tars-chat .
#
# deploy/docker/compose.yaml runs it as two processes over one folder: config.local.toml, .env and voice_data/ are the
# household's, mounted in; the image holds only the code and config.toml's defaults. A new version is a new image:
# TARS's own updater ([updates]) is for a git checkout on a Pi, and stays off here.

FROM ghcr.io/astral-sh/uv:0.12.23-trixie-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/python UV_PYTHON_PREFERENCE=only-managed
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 tars
COPY --from=build /opt/python /opt/python
COPY --from=build --chown=tars:tars /app /app
COPY --chown=tars:tars config.toml /app/config.toml
USER tars
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
# The processes are run straight from the environment, not with `uv run`, which would want to write to it.
ENTRYPOINT ["voice-assistant"]
CMD ["--telegram"]
