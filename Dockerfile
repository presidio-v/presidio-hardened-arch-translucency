# Reproducible, non-root image for the read-only `pat export` endpoint.
# The package is built from the checked-out release source, so container
# publication cannot race PyPI availability or install a different artifact.
FROM ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec AS uv

FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4 AS builder

WORKDIR /build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4

ARG VERSION="0.23.0"
ARG VCS_REF=""
LABEL org.opencontainers.image.title="pat-exporter" \
      org.opencontainers.image.description="Read-only architectural-translucency Prometheus exporter (pat export)" \
      org.opencontainers.image.source="https://github.com/presidio-v/presidio-hardened-arch-translucency" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="MIT" \
      maintainer="presidio-v"

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN useradd --create-home --uid 10001 --user-group patuser

USER patuser
WORKDIR /home/patuser
EXPOSE 9847

ENTRYPOINT ["pat"]
CMD ["export", "--requests-per-second", "500", "--avg-latency-ms", "80", \
     "--current-layer", "container", "--once"]
