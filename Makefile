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
