FROM node:24-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY --from=oven/bun:1.4.0 /usr/local/bin/bun /usr/local/bin/bun
ENV UV_PYTHON=3.11

WORKDIR /privana
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY solidity/package.json solidity/bun.lock solidity/hardhat.config.ts solidity/tsconfig.json ./solidity/
COPY solidity/contracts ./solidity/contracts
COPY solidity/tasks ./solidity/tasks
RUN cd solidity && bun install --frozen-lockfile && npx hardhat compile
COPY src ./src
RUN uv sync --frozen --no-dev
COPY .env.localnet .env
COPY docker/localnet/bootstrap.py /localnet/bootstrap.py
