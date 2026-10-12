# The bot as one always-on container. Build from this directory: docker build -t hftbot .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY hftbot ./hftbot
RUN pip install ".[solana]"
COPY config.solana.toml config.example.toml ./

# Run as an unprivileged user; journals and recordings go to the /data volume.
RUN useradd --create-home --uid 10001 bot && mkdir /data && chown bot /data
USER bot
VOLUME /data

ENTRYPOINT ["hftbot"]
# Default: paper trading on live SOL/USDC data. No wallet, no orders. Needs HFTBOT_RPC_URL.
CMD ["run", "--config", "config.solana.toml", "--journal", "/data/paper.jsonl"]
