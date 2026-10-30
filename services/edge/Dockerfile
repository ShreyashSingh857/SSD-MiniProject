FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends redis-server \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/edge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/cdncore ./cdncore
COPY shared/start-with-redis.sh ./start.sh
COPY services/edge/edge_app.py .
RUN useradd -m -u 1000 app
USER app
ENV PYTHONUNBUFFERED=1
CMD ["sh", "start.sh", "edge_app:app"]
