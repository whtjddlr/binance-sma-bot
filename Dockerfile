FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    addgroup --system bot && \
    adduser --system --ingroup bot bot

COPY . .
RUN python -m pip install . && \
    mkdir -p /app/data && \
    chown -R bot:bot /app/data

USER bot
VOLUME ["/app/data"]

CMD ["portfolio-paper", "run"]
