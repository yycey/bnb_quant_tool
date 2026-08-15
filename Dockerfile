FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p data/logs data/locks \
    && chmod +x deploy/linux/run_headless.sh

# 默认跑 autopilot；compose 里 watcher 另起服务
CMD ["python", "autopilot_daemon.py", "--no-embed-watcher"]
