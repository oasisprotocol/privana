# Main application stage
FROM python:3.11-alpine

WORKDIR /app

ARG ENV_FILE

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
COPY ${ENV_FILE} .env
RUN uv sync --no-dev --frozen --no-install-project

COPY . .

# Verify Solidity artifacts exist (run 'make solidity-build' locally if this fails)
RUN test -f solidity/artifacts/contracts/Accounting.sol/Accounting.json \
    && test -f solidity/artifacts/contracts/auth/AccountingSiweAuth.sol/AccountingSiweAuth.json \
    || (echo "ERROR: Solidity artifacts not found. Run 'make solidity-build' before building Docker image." && exit 1)

RUN uv sync --no-dev --frozen

ENV PYTHONPATH="/app"

CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
