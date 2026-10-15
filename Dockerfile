# ghcr.io/astral-sh/uv:python3.12-bookworm-slim (uv 0.9.30)
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /opt/stdio-box

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY schemas ./schemas

RUN uv sync --frozen --no-dev --no-editable

# ghcr.io/astral-sh/uv:0.13.1 — distroless image, binary at /uv
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

# node:22-bookworm-slim
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node

# python:3.12-slim-bookworm
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 box \
    && useradd --uid 10001 --gid 10001 \
        --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin box \
    && groupadd --gid 10002 box-install \
    && useradd --uid 10002 --gid 10002 \
        --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin box-install \
    && install -d -o 10001 -g 10001 -m 0700 /data \
    && install -d -o 10002 -g 10002 -m 0755 /cache \
    && install -d -o root -g root -m 0711 /tmp/box \
    && mkdir -p /licenses/python /licenses/node /licenses/uv \
    && cp /usr/local/lib/python3.12/LICENSE.txt /licenses/python/LICENSE.txt

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=uv /uvx /usr/local/bin/uvx
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
# npm and npx are relative symlinks. Copying them as files breaks require().
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=node /usr/local/LICENSE /licenses/node/LICENSE
# The distroless uv image ships the binary only. Apache-2.0 is the text in the
# uv builder image. uv is also MIT; that file is not in either uv image.
COPY --from=builder /usr/share/common-licenses/Apache-2.0 /licenses/uv/Apache-2.0

COPY --from=builder /opt/stdio-box/.venv /opt/stdio-box/.venv
COPY schemas /opt/stdio-box/schemas
COPY packages/ /opt/box/packages/

ENV PATH="/opt/stdio-box/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8081

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["curl", "-fsS", "http://127.0.0.1:8081/healthz"]

LABEL org.opencontainers.image.licenses="Apache-2.0"

ENTRYPOINT ["/usr/bin/tini", "--", "stdio-box", "launch"]
