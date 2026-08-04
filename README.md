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

### Staging (Sapphire Testnet)

- **API**: https://api.testnet.privana.finance
- **Accounting Proxy**: `0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f`

See [solidity/README.md](solidity/README.md) for full contract addresses and deployment instructions.
