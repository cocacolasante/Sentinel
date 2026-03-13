FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /deploy
COPY webhook_server.py deploy.sh ./
RUN chmod +x deploy.sh

CMD ["python", "/deploy/webhook_server.py"]
