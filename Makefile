solidity: solidity-build solidity-test

solidity-build:
	cd solidity && npm run build

solidity-test:
	cd solidity && npm run test

solidity-clean:
	cd solidity && rm -rf dist artifacts cache typechain-types ignition/deployments

solidity-coverage:
	cd solidity && npm run coverage
