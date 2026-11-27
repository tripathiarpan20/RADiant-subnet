# RADiant validator image: api, migrations, seed and (later) watcher, worker,
# round-worker and weights all run from this one image with different commands.
FROM python:3.12.16-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra validator --extra chain --extra sdk --no-install-project && rm /usr/local/bin/uv

COPY config.py ./
COPY api ./api
COPY build ./build
COPY campaign ./campaign
COPY chain ./chain
COPY db ./db
COPY gate ./gate
COPY radiant_sdk ./radiant_sdk
COPY storage ./storage
COPY worker ./worker

RUN useradd --system --uid 10001 --no-create-home radiant && mkdir -p /scratch && chown radiant /scratch
USER radiant
ENV HOME=/tmp
