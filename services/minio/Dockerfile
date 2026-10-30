# Upstream MinIO stopped publishing images (repo archived 2026); pgsty/minio is a maintained
# community build of the same server.
FROM pgsty/minio:RELEASE.2026-06-18T00-00-00Z
COPY services/minio/start.sh /start.sh
ENTRYPOINT ["sh", "/start.sh"]
