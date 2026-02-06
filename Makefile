.PHONY: install dev run test lint typecheck clean solidity solidity-build solidity-test solidity-lib solidity-clean solidity-coverage

install:
	uv sync --no-dev

dev:
	uv sync

run:
	uv run python -m src.main

test:
	uv run pytest

lint:
	uv run ruff check src

typecheck:
	uv run mypy src

lint-fix:
	uv run ruff check --fix src

clean:
	rm -rf .venv __pycache__ .pytest_cache .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

solidity: solidity-build solidity-test

solidity-lib:
	cd solidity && git submodule update --init --recursive
	cd solidity/lib/hashi && bun install

solidity-build:
	cd solidity && bun run build

solidity-test:
	cd solidity && bun run test

solidity-clean:
	cd solidity && rm -rf dist artifacts cache typechain-types ignition/deployments

solidity-coverage:
	cd solidity && bun run coverage
