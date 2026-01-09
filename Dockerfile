FROM python:3.11-alpine

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

COPY . .
RUN uv sync --no-dev --frozen

ENV PYTHONPATH="/app"

CMD ["uv", "run", "python", "-m", "src.main"]
