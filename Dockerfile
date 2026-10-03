FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ghostscript ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app.py .

RUN pip install --no-cache-dir fastapi uvicorn python-multipart pillow pypdf pillow-heif

EXPOSE 10000
# --limit-concurrency keeps a burst of big jobs from piling up in this
# box's memory (it answers 503 beyond the limit instead of OOM-killing).
CMD python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --limit-concurrency 2
