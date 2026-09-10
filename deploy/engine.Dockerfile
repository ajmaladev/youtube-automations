# Video engine image, equivalent to vendor/video-engine/Dockerfile but on
# Debian 12 (bookworm). Upstream's python:3.11-slim-bullseye base stopped building
# after Debian 11 LTS ended (Aug 2026): bullseye-security's Release file expired,
# so `apt-get update` fails. Build context is still the untouched vendor directory.
FROM python:3.11-slim-bookworm

WORKDIR /app
RUN chmod 777 /app
ENV PYTHONPATH="/app"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 3 --timeout 60 -r requirements.txt

COPY . .

EXPOSE 8080 8501
