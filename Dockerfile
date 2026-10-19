# One image: the API, with the built frontend served from the same origin.

# ---- frontend ----------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Baked into the bundle at build time (it is a public URL, not a secret).
ARG VITE_NEON_AUTH_URL=https://ep-fragrant-cloud-b39d31yk.neonauth.c-4.ap-southeast-1.aws.neon.tech/neondb/auth
ENV VITE_NEON_AUTH_URL=$VITE_NEON_AUTH_URL
RUN npm run build

# ---- backend -----------------------------------------------------------
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY glossaries ./glossaries
RUN uv sync --frozen --no-dev

COPY --from=web /web/dist ./static

ENV PATH="/app/.venv/bin:$PATH" \
    SQLAGENT_STATIC_DIR=/app/static \
    SQLAGENT_DATA_DIR=/var/data

RUN mkdir -p /var/data
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn sqlagent.api.app:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
