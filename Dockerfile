FROM python:3.12-slim

WORKDIR /app
COPY odds_watcher/ ./odds_watcher/

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/odds_watcher.db
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "odds_watcher"]
CMD ["run"]
