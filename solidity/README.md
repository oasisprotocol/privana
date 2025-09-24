# ROFL Accounting

TBD

## Compilation

Execute the following to compile the contracts and the typescript tests:

```shell
npm install
npm build
```

## Testing

To run tests on a Hardhat node, run:

```shell
npm test
``` 

To also run confidential tests, you need to spin up a Localnet Sapphire node.
For example in the Docker:

```shell
docker run -it -p8545:8545 -p8546:8546 ghcr.io/oasisprotocol/sapphire-localnet -test-mnemonic -n 5
```

Then, let tests use the Localnet network:

```shell
npm run test --network sapphire-localnet
```

## Registering and deploying

```shell
npx hardhat deploy --network sapphire-localnet
npx hardhat addEVMNativeToken --network sapphire-localnet --chainid 1234 --address <deployed accounting address>
```

npx hardhat addEVMNativeToken --network sapphire-localnet --chainid 1234 --address 0x5FbDB2315678afecb367f032d93F642f64180aa3