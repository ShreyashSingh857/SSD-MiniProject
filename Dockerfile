# Single image for the whole CDN.
# - Hugging Face Spaces / any single-container host: runs everything under supervisord
#   (4 Redis instances, MinIO, origin, shield, 3 edges, router on $PORT).
# - docker-compose.yml reuses this image and overrides the command per service.
# Upstream MinIO stopped publishing images/binaries (repo archived 2026); pgsty/minio is a
# maintained community build of the same server.
FROM pgsty/minio:RELEASE.2026-06-18T00-00-00Z AS minio

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends redis-server fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=minio /usr/bin/minio /usr/local/bin/minio

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cdn ./cdn
COPY deploy ./deploy

# Hugging Face runs containers as uid 1000.
RUN useradd -m -u 1000 user && mkdir -p /tmp/cdn && chown -R user /app /tmp/cdn
USER user
ENV PYTHONUNBUFFERED=1 PORT=7860
EXPOSE 7860
CMD ["supervisord", "-c", "/app/deploy/supervisord.conf"]
