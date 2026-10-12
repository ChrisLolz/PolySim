FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/polysim.sqlite3

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir . \
    && groupadd --system --gid 10001 polysim \
    && useradd --system --uid 10001 --gid polysim polysim \
    && mkdir -p /data \
    && chown polysim:polysim /data

USER polysim
VOLUME ["/data"]

CMD ["python", "-m", "polysim"]
