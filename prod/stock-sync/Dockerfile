# syntax=docker/dockerfile:1

FROM node:24.19.0-bookworm-slim AS receiver

ENV NODE_ENV=production
WORKDIR /app/prod/stock-sync

RUN groupadd --gid 10001 stock-sync \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin stock-sync

COPY --chown=10001:10001 package.json receiver.js schema.sql ./

USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "receiver.js"]

FROM receiver AS receiver-tests

COPY --chown=10001:10001 README.md ./
COPY --chown=10001:10001 tests/test_receiver.js ./tests/test_receiver.js

HEALTHCHECK NONE
CMD ["node", "--test", "tests/test_receiver.js"]

FROM python:3.14.6-slim-trixie AS worker

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app/prod/stock-sync

RUN groupadd --gid 10001 stock-sync \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin stock-sync

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 worker.py meli_oauth.py schema.sql ./
COPY --chown=10001:10001 stock_sync ./stock_sync

USER 10001:10001
ENTRYPOINT ["python", "worker.py"]
CMD ["run"]

FROM worker AS worker-tests

USER root
RUN python -m pip install --no-cache-dir "pytest>=8,<10"
COPY --chown=10001:10001 tests ./tests
USER 10001:10001

ENTRYPOINT ["python", "-m", "pytest"]
CMD ["tests", "-q", "-p", "no:cacheprovider"]
