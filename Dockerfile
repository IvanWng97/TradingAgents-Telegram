FROM python:3.14-slim

# git is needed because pyproject installs tradingagents from a git URL
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# -u: unbuffered stdout so `docker logs` shows output immediately
CMD ["python", "-u", "-m", "tg_bot"]
