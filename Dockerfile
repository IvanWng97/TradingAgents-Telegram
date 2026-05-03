FROM python:3.14-slim

# git is needed because requirements.txt installs tradingagents from a git URL
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer is cached when only app code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY bot.py config.py storage.py utils.py analysis.py ./
COPY handlers ./handlers

# -u: unbuffered stdout so `docker logs` shows output immediately
CMD ["python", "-u", "bot.py"]
