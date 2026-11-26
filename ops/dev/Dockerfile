# Dev/QA image for mirror-api. Build context is server/ (never the repo root, so
# .env is not sent to the Docker daemon).
FROM python:3.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --group dev

COPY . .

EXPOSE 8000
CMD ["uvicorn", "--factory", "mirror_api.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
