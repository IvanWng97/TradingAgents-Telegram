# Multi-stage build. `git` is needed to pip-install tradingagents from a git
# URL but isn't needed at runtime — keeping it out of the final image drops
# ~80 MB and most of the package-CVE surface Trivy flags. The builder writes
# the dependency tree into a venv at /opt/venv; the runtime stage copies the
# whole venv and uses it as its Python environment.
FROM python:3.14-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .


FROM python:3.14-slim AS runtime

# Same PATH override so `python` and `pip` resolve to the venv's interpreter.
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# -u: unbuffered stdout so `docker logs` shows output immediately.
CMD ["python", "-u", "-m", "tg_bot"]
