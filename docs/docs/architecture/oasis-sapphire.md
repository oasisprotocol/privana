---
id: oasis-sapphire
title: "Oasis Sapphire"
sidebar_position: 1
description: "Oasis Sapphire is the confidential blockchain Privana runs on — an EVM platform whose smart contracts execute inside Intel SGX enclaves."
---

Oasis Sapphire is the confidential blockchain that everything in Privana runs on. It provides the hardware-secured foundation that makes private, non-custodial DeFi possible.

**[Oasis Sapphire](https://docs.oasis.io/dapp/sapphire/)** is a smart contract platform built on the [Oasis Network](https://oasis.net/) that runs the Ethereum Virtual Machine (EVM) entirely within Intel SGX enclaves. This means smart contract logic *and* its state are confidential by default — transactions are encrypted, contract storage is encrypted, and even the validators running the network cannot read what's happening inside a contract.

Sapphire provides several properties that are critical to Privana:

### Confidential State

Contract storage is encrypted on-chain and in memory. State is accessible only to the executing TEE. This is what keeps your balances, policies, and trade history private. [Learn more →](https://docs.oasis.io/dapp/sapphire/)

### Rollback Protection

Keys inside the enclave cannot be "rewound" to a previous state — preventing attacks where someone rolls back enclave state to re-use a spent nonce. Sapphire handles this at the platform level.

### Off-Chain Simulation

Sapphire functions can be invoked via free, off-chain queries that simulate transactions without modifying storage. Both on-chain and off-chain invocations run inside the TEE and require signed authentication — enabling cost-efficient policy evaluation.

### EVM Compatibility

Sapphire supports standard Solidity smart contracts. Developers don't need specialized TEE knowledge to build on it — the confidentiality layer is handled by the platform.

### ROFL: Runtime Off-chain Logic

Privana also uses **[ROFL](https://docs.oasis.io/build/rofl/)** (Runtime Off-chain Logic) — containers running inside a TEE that are attested on-chain by Oasis Sapphire. The Privana SDK exposes its REST and WebSocket API through ROFL, meaning the entire communication channel between the Privana app and the Privana service runs inside attested, TEE-secured containers. Sapphire can seamlessly verify that EVM transactions originate from a specific ROFL instance.

### Cross-chain signing

Here's what makes this architecture powerful for users: an Oasis Sapphire contract running inside a TEE can **sign valid transactions for any external chain**. Your Ethereum swap is signed inside an Oasis Sapphire enclave and submitted directly to Ethereum — never passing through a public Ethereum mempool. The target chain receives a completed, signed transaction. There is no window in which bots can see your intent.

For more technical detail, see the [Oasis Sapphire developer documentation](https://docs.oasis.io/dapp/sapphire/) and the [Oasis Protocol overview](https://oasis.net/).
