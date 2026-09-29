# syntax=docker/dockerfile:1
# Multi-arch (linux/amd64, linux/arm64): works on Apple Silicon & Intel Macs via Docker Desktop and on Linux servers.
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /app app
WORKDIR /app
COPY pyproject.toml README.md ./
COPY propguard ./propguard
# Optional: behind a TLS-intercepting proxy pass its CA bundle as a build secret (never stored in the image):
#   docker build --secret id=extra_ca,src=/path/to/ca-bundle.crt .
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install ".[postgres,llm]"
COPY seed ./seed
COPY tests ./tests
COPY examples ./examples
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /data && chown -R app:app /data /app
USER app
ENV PROPGUARD_DATA_DIR=/data PROPGUARD_ACCEPTANCE_MARKER=/data/acceptance.json
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
