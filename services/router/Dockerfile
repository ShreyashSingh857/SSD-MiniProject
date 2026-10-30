FROM python:3.12-slim
WORKDIR /app
COPY services/router/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/cdncore ./cdncore
COPY services/router/router_app.py services/router/bench.py ./
RUN useradd -m -u 1000 app
USER app
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec uvicorn router_app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
