FROM python:3.11-slim

WORKDIR /app

COPY app/ ./app/
COPY web/ ./web/
COPY verify/ ./verify/

ENV PORT=8000 \
    TMS_DB_PATH=/data/tms.db \
    PAGE_DIR=/srv/page \
    TMS_ALLOW_RESET=0 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python", "-m", "app.server"]
