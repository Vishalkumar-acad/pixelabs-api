FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ghostscript ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app.py .

RUN pip install --no-cache-dir fastapi uvicorn python-multipart pillow pypdf pillow-heif

EXPOSE 10000
# Memory is guarded inside the app: app.py holds a single "heavy" slot, so
# only one Ghostscript/PIL/ffmpeg job runs at a time. --limit-concurrency is
# now just a backstop against a flood of connections — keep it well above the
# number of concurrent jobs you expect, so a light route like /health never
# gets a 503 while heavy work is running.
CMD python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --limit-concurrency 16
