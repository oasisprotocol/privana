# Main application stage
FROM python:3.11-alpine

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

COPY . .

# Verify Solidity artifacts exist (run 'make solidity-build' locally if this fails)
RUN test -f solidity/artifacts/contracts/Accounting.sol/Accounting.json \
    || (echo "ERROR: Solidity artifacts not found. Run 'make solidity-build' before building Docker image." && exit 1)

RUN uv sync --no-dev --frozen

ENV PYTHONPATH="/app"

CMD ["uv", "run", "python", "-m", "src.main"]
