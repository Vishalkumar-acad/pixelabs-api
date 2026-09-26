FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ghostscript && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app.py .

RUN pip install --no-cache-dir fastapi uvicorn python-multipart pillow pypdf pillow-heif

EXPOSE 10000
CMD python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}
