# Build a small reverse proxy so Render's single public port can serve both the S3 API and
# the MinIO Console. The proxy is only used when MINIO_CONSOLE_PROXY=on.
FROM golang:1.24-alpine AS proxy-build
WORKDIR /src
COPY services/minio/proxy.go ./proxy.go
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /minio-proxy ./proxy.go

# Upstream MinIO stopped publishing images (repo archived 2026); pgsty/minio is a maintained
# community build of the same server.
FROM pgsty/minio:RELEASE.2026-06-18T00-00-00Z
COPY --from=proxy-build /minio-proxy /minio-proxy
COPY services/minio/start.sh /start.sh
ENTRYPOINT ["sh", "/start.sh"]
