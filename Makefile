solidity: solidity-build solidity-test

solidity-lib:
	cd solidity && git submodule update --init --recursive
	cd solidity/lib/hashi && npm install

solidity-build:
	cd solidity && npm run build

solidity-test:
	cd solidity && npm run test --network sapphire-localnet

solidity-clean:
	cd solidity && rm -rf dist artifacts cache typechain-types ignition/deployments

solidity-coverage:
	cd solidity && npm run coverage --network sapphire-localnet
