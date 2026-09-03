# Privana

A reusable framework for apps running in ROFL that enables:

- Seamless crosschain deposits and withdrawals.
- Private accounting of user funds.
- Web2 user experience with Web3 security and trustlessness.

## Project Layout

| Path | What's there |
|------|--------------|
| [`solidity/`](solidity/README.md) | On-chain contracts (Accounting, EVMSignerAndVerifier, AccountingSiweAuth), deployment & Hardhat tasks. |
| [`src/`](src/README.md) | Python service that runs in the ROFL TEE — deposit verification, sweep state machine, withdrawal resolution. |
| [`docs/api-reference.md`](docs/api-reference.md) | HTTP API reference: request/response shapes, auth, error codes. |

## Deployments

The Privana python backend service is containerized and deployed as an
application running in ROFL TEE.

See [solidity/README.md](solidity/README.md#contract-addresses) for a full list
of contract addresses and ROFL instances.
