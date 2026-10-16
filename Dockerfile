FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends restic ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG BUILD_SHA=development
ENV WORTLING_BUILD_SHA=$BUILD_SHA
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --uid 10001 --create-home wortling && mkdir /data && chown wortling /data
USER wortling
RUN /app/.venv/bin/python -c "from importlib.resources import files; p=files('wortling'); p.joinpath('trial.html').read_text(); p.joinpath('setup.html').read_text()"
ENV WORTLING_CONFIG=/app/config.yaml WORTLING_DATA_DIR=/data
ENTRYPOINT ["/app/.venv/bin/wortling"]
CMD ["serve", "--host", "0.0.0.0"]
