# Hardened image for the read-only `pat export` Prometheus endpoint.
#
# The exporter is emit-only (arc invariant A1): it serves GET /metrics and
# /healthz and never mutates infrastructure. The image runs as an unprivileged
# user and carries only the runtime needed to serve metrics.
#
# Build the latest published package image:
#   docker build -t ghcr.io/presidio-v/pat-exporter:0.17.0 .
# Build the image expected by charts/pat-exporter 0.16.0:
#   docker build --build-arg PAT_VERSION=0.16.0 -t ghcr.io/presidio-v/pat-exporter:0.16.0 .
# v0.17.0 is an evidence/library release; the Helm chart remains on appVersion
# 0.16.0 until a chart/image release is cut deliberately.
FROM python:3.12-slim

ARG PAT_VERSION=""

LABEL org.opencontainers.image.title="pat-exporter" \
      org.opencontainers.image.description="Read-only architectural-translucency Prometheus exporter (pat export)" \
      org.opencontainers.image.source="https://github.com/presidio-v/presidio-hardened-arch-translucency" \
      org.opencontainers.image.licenses="MIT" \
      maintainer="presidio-v"

# Install the published package. PAT_VERSION pins a release when set.
# --no-cache-dir keeps the layer slim; no build toolchain is retained.
RUN pip install --no-cache-dir \
      "presidio-hardened-arch-translucency${PAT_VERSION:+==$PAT_VERSION}"

# Unprivileged runtime user (matches the chart's runAsUser default).
RUN useradd --create-home --uid 10001 --user-group patuser
USER patuser
WORKDIR /home/patuser

EXPOSE 9847

# The container serves metrics; the chart supplies the workload flags as args.
# Default args make a bare `docker run` print the exposition once and exit.
ENTRYPOINT ["pat"]
CMD ["export", "--requests-per-second", "500", "--avg-latency-ms", "80", \
     "--current-layer", "container", "--once"]
