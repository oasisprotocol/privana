# Accounting Module - Solidity Contracts

A cross-chain accounting system built on Oasis Sapphire that enables secure deposits, transfers, and withdrawals across multiple chains. The system uses cryptographic transaction proofs for deposit verification and EIP-712 signatures for user authorization.

## Overview

The Accounting module consists of three main components:

- **Accounting.sol** - Core accounting contract managing user balances and operations
- **EVMSignerAndVerifier.sol** - EVM transaction verification and signing capabilities  
- **EIP712SignatureVerifier.sol** - User authorization via EIP-712 signatures

### Key Features

- **Multi-chain Deposits**: Verify and credit deposits from any EVM chain using transaction inclusion proofs
- **Fund Locking**: Escrow-like functionality for service interactions with time-bounded locks
- **P2P Transfers**: Internal transfers between users without on-chain transactions
- **Secure Withdrawals**: Generate signed transactions for withdrawing funds to origin chains
- **Universal Token Support**: Native tokens (ETH, MATIC, BNB) and ERC20 tokens across chains

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   User Wallet   │    │   Accounting     │    │  ShoyuBashi     │
│                 │    │   Contract       │    │  Oracle         │
├─────────────────┤    ├──────────────────┤    ├─────────────────┤
│ • EIP712 Sigs   │───▶│ • Balance Mgmt   │───▶│ • Block Hashes  │
│ • Tx Proofs     │    │ • Fund Locking   │    │ • Cross-chain   │
│ • Withdrawals   │◀───│ • Verification   │    │   Validation    │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## Installation

```shell
bun install
```

## Compilation

Compile the contracts and generate TypeScript bindings:

```shell
bun run build
```

This will:
- Compile Solidity contracts using Hardhat
- Generate TypeChain TypeScript bindings
- Create artifacts in `artifacts/` and `typechain-types/` (ABIs for)

## Testing

### Local Hardhat Testing

Run tests on a local Hardhat node:

```shell
bun test
```

### Sapphire Localnet Testing

For confidential computing features (generating wallet, signing), run tests on Sapphire Localnet:

1. Start the Sapphire Localnet container:

```shell
docker run -it --rm -p8544-8548:8544-8548 ghcr.io/oasisprotocol/sapphire-localnet -to "<mnemonic from hardhat.config.ts>"
```

2. Run tests against Sapphire Localnet:

```shell
bun run test -- --network sapphire-localnet
```

### Test Coverage

Generate test coverage reports:

```shell
bun run coverage
```

## Deployment

### Deploy to Sapphire Localnet

```shell
npx hardhat deploy --network sapphire-localnet
```

### Deploy to Sapphire Testnet

```shell
npx hardhat deploy --network sapphire-testnet
```

## Configuration

### Adding Token Support

After deployment, register tokens that the accounting system should support:

#### Native Token (ETH, MATIC, etc.)

```shell
npx hardhat addEVMNativeToken --network sapphire-localnet --chainid 1 --address <deployed-accounting-address>
```

#### ERC20 Token

```shell
npx hardhat addEVMERC20Token --network sapphire-localnet --chainid 1 --token-address 0x... --address <deployed-accounting-address>
```

### Setting Gas Prices

Configure gas prices for different chains:

```shell
npx hardhat setGasPrice --network sapphire-localnet --chainid 1 --gas-price 20000000000 --address <deployed-accounting-address>
```

## Usage Examples

### Deposit Flow

1. User sends tokens to the accounting contract's EVM address on any supported chain
2. User generates transaction inclusion proof using block header and Merkle proof
3. User calls `includeEVMDeposit()` with proof to credit their balance

### Transfer Flow

1. User signs EIP-712 transfer message
2. Anyone can submit the signature to execute the transfer
3. Balances are updated atomically within the accounting system

### Withdrawal Flow

1. User signs EIP-712 withdrawal message  
2. Contract generates signed transaction for the origin chain
3. Signed transaction is broadcast to complete the withdrawal

## Contract Addresses

| Network | Contract | Address |
|---------|----------|---------|
| Sapphire Testnet | Accounting | TBD |
| Sapphire Mainnet | Accounting | TBD |

## Security Considerations

- Transaction proofs are verified using Merkle Patricia Trie validation
- All user operations require EIP-712 signatures to prevent unauthorized access
- Block hashes are verified through the ShoyuBashi oracle system
- Private keys for withdrawal signing are managed securely within Sapphire's confidential environment

## Development

### Project Structure

```
contracts/
├── Accounting.sol              # Main accounting contract
├── EVMSignerAndVerifier.sol    # EVM transaction handling
├── __(Network)SignerAndVerifier.sol    # Sui, Solana, etc transaction handling
├── EIP712SignatureVerifier.sol # User authorizations and signatures
├── Types.sol                   # Shared data structures
├── interfaces/                 # External contract interfaces
└── lib/                       # Utility libraries

test/
├── Accounting.E2E.ts          # End-to-end integration test
├── EVMSignerAndVerifier.ts    # EVM functionality tests
└── utils.ts                   # Test utilities
```

### Key Dependencies

- **Hardhat** - Development environment and testing framework
- **Oasis Sapphire Contracts** - Confidential computing primitives
- **OpenZeppelin** - Security-audited contract libraries
- **Solidity RLP** - RLP encoding/decoding for Ethereum data