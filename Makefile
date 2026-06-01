.PHONY: install dev run test lint typecheck clean solidity solidity-build solidity-test solidity-ci solidity-lib solidity-clean solidity-coverage format format-check openapi openapi-check

install:
	uv sync --no-dev

dev:
	uv sync
	-uv run pre-commit install

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

openapi:
	uv run python scripts/gen_openapi.py > docs/openapi.json

openapi-check:
	uv run python scripts/gen_openapi.py --check

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

# Mirrors the solidity job in .github/workflows/ci.yml: clean compile,
# size gate, upgrade-safety, targeted storage-layout pre-flight, full suite.
# Run before pushing if you want CI-equivalent verification locally.
solidity-ci: solidity-clean solidity-build
	cd solidity && bun run check:size
	cd solidity && npx hardhat run scripts/validate-upgrade.ts
	cd solidity && bun run test -- --bail --grep "storage|fallback dispatcher|setBridgeModule|signer isolation"
	cd solidity && bun run test

solidity-clean:
	cd solidity && rm -rf dist artifacts cache typechain-types ignition/deployments

solidity-coverage:
	cd solidity && bun run coverage
