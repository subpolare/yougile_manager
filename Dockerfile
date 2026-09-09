FROM ghcr.io/astral-sh/uv:0.12.11 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY README.md ./README.md

RUN useradd --create-home --uid 10001 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["uv", "run", "--no-dev", "python", "-m", "app.main"]
