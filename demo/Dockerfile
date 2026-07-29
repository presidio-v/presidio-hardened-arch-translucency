# Pinned by digest (same base as the root Dockerfile) so the demo workload is
# reproducible and the image cannot drift under the floating tag.
FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4
LABEL maintainer="presidio-v" \
      description="Monte Carlo π workload server for pat demo"
WORKDIR /app
COPY app.py .
# Run as an unprivileged user to limit blast radius if the workload is compromised.
RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status==200 else 1)"]
CMD ["python", "-u", "app.py"]
