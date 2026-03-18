.PHONY: install dev run test lint typecheck clean solidity solidity-build solidity-test solidity-lib solidity-clean solidity-coverage format format-check

install:
	uv sync --no-dev

dev:
	uv sync

run:
	uv run python -m src.main

test:
	DISABLE_ROFL_KEYS=1 uv run pytest

lint:
	uv run ruff check src test

typecheck:
	uv run mypy src

lint-fix:
	uv run ruff check --fix src test

format:
	uv run ruff format src test

format-check:
	uv run ruff format --check src test

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
