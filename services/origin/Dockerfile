FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/origin/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/cdncore ./cdncore
COPY services/origin/origin_app.py .
COPY services/origin/content ./content
# Render the JPEGs now: small hosted instances would need minutes for this at every start.
RUN python -c "from content import generator; generator.prerender_images()"
RUN useradd -m -u 1000 app
USER app
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec uvicorn origin_app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --log-level warning"]
