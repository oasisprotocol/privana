build:
	@echo "Building ROFL application..."
	@rsync -a --exclude='.git' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' --exclude='venv' ./ /tmp/accounting-module-build/
	@docker run --platform linux/amd64 --volume /tmp/accounting-module-build:/src ghcr.io/oasisprotocol/rofl-dev:main oasis rofl build
	@cp /tmp/accounting-module-build/accounting-module.default.orc /tmp/accounting-module-build/rofl.yaml ./
	@rm -rf /tmp/accounting-module-build
	@echo "Build complete: accounting-module.default.orc, rofl.yaml"

solidity: solidity-build solidity-test

solidity-build:
	cd solidity && npm run build

solidity-test:
	cd solidity && npm run test

solidity-clean:
	cd solidity && rm -rf dist artifacts cache typechain-types ignition/deployments

solidity-coverage:
	cd solidity && npm run coverage
