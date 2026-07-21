# Contributing

Thank you for your interest in contributing! This repository hosts the ROFL
Accounting Module: on-chain contracts in [`solidity/`](solidity/) and the
Python service in [`src/`](src/) that runs in Oasis ROFL TEE.

## Getting started

- Python service: see [`src/README.md`](src/README.md). The project uses
  [uv](https://docs.astral.sh/uv/) for dependency management and `pytest` for
  tests.
- Contracts: see [`solidity/README.md`](solidity/README.md). The project uses
  Hardhat with [bun](https://bun.sh/) for dependency management.

## Before opening a pull request

- Install the pre-commit hooks and make sure they pass:

  ```sh
  pre-commit install
  pre-commit run --all-files
  ```

- Run the relevant test suites (`pytest` for Python, `bun hardhat test` in
  `solidity/` for contracts).
- Keep pull requests focused; separate refactors from behavior changes.
- Use descriptive commit messages with a component prefix, e.g.
  `solidity: ...`, `api: ...`, `rofl: ...`, matching the existing history.

## Reporting issues

Bug reports and feature requests are welcome via GitHub issues. For anything
security-sensitive, please follow [SECURITY.md](SECURITY.md) instead of opening
a public issue.
