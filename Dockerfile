# Main application stage
FROM python:3.11-alpine

WORKDIR /app

ARG ENV_FILE

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY README.md pyproject.toml uv.lock ./
COPY ${ENV_FILE} .env
RUN uv sync --no-dev --frozen --no-install-project

# Python backend.
COPY src ./src

# Contract artifacts.
COPY solidity/artifacts/contracts/Accounting.sol/Accounting.json ./solidity/artifacts/contracts/Accounting.sol/Accounting.json
COPY solidity/artifacts/contracts/auth/AccountingSiweAuth.sol/AccountingSiweAuth.json ./solidity/artifacts/contracts/auth/AccountingSiweAuth.sol/AccountingSiweAuth.json

RUN uv sync --no-dev --frozen

ENV PYTHONPATH="/app"

CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
