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
