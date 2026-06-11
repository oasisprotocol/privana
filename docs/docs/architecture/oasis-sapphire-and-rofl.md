---
id: oasis-sapphire-and-rofl
title: "Oasis Sapphire and ROFL"
sidebar_position: 1
description: "Oasis Sapphire is the confidential EVM blockchain Privana runs on, and ROFL is the framework that runs Privana's off-chain service in attested TEEs."
---

Oasis Sapphire is the confidential EVM blockchain Privana runs on, and ROFL is the framework that runs Privana's off-chain service in attested TEEs.

**[Oasis Sapphire](https://docs.oasis.io/build/sapphire/)** is a smart contract platform built on [Oasis](https://oasis.net/) that runs the Ethereum Virtual Machine (EVM) entirely within Intel SGX enclaves. This means smart contract logic *and* its state are confidential by default. Transactions are encrypted, contract storage is encrypted, and even the validators running the network cannot read what's happening inside a contract.

Sapphire provides several properties that are critical to Privana:

## Confidential State

Contract storage is encrypted on-chain and in memory. State is accessible only to the executing TEE. This is what keeps your balances, policies, and trade history private. [Learn more →](https://docs.oasis.io/build/sapphire/)

## EVM Compatibility

Sapphire supports standard Solidity smart contracts. Developers don't need specialized TEE knowledge to build on it. The confidentiality layer is handled by the platform. Because they are ordinary smart contracts, they can be independently audited, so you can verify the on-chain logic does exactly what it claims.

## Cross-chain signing

A Sapphire contract holds a private key inside the enclave and signs transactions for external chains. The signing happens on Sapphire. The key never leaves the enclave, and the contract logic that decides what to sign stays confidential. The Privana service then broadcasts the finished transaction. What the destination chain sees is an ordinary signed transaction from the pooled vault address, with no link back to you.

## ROFL: Runtime Off-chain Logic

Privana also uses **[ROFL](https://docs.oasis.io/build/rofl/)** (Runtime Off-chain Logic): containers running inside a TEE that are attested on-chain by Oasis Sapphire. The Privana service, which exposes the REST API the app calls, runs inside a ROFL container, so its off-chain logic executes in an attested, TEE-secured environment. Sapphire can verify that EVM transactions originate from that specific ROFL instance.

For more technical detail, see the [Oasis Sapphire developer documentation](https://docs.oasis.io/build/sapphire/) and the [Oasis Protocol overview](https://oasis.net/).
