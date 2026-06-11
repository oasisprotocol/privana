---
id: deep-dive
title: "Architecture Deep Dive"
sidebar_position: 2
description: "The full Privana architecture, from the layers that make it up to the components that handle your trades and policies."
---

Privana has five parts. Integrators build with the client SDK, which calls the Privana service over REST. The service is the hub: it reads and writes the Oasis Sapphire contracts (the confidential root of trust that holds the keys and signs transactions), and it verifies and settles transactions on the external chains where liquidity lives.

![Privana architecture as a trust spine. A vertical column runs from the integrators (the Privana app and third-party apps) down through the client SDK and the Privana service to Oasis Sapphire, the confidential root of trust that holds the keys and signs transactions. Each part uses the one below it: integrators build with the SDK, the SDK calls the service over REST, and the service reads and writes the Sapphire contracts. Off to the side, the same service also verifies and settles transactions on the external chains (Ethereum, Base, and HyperEVM) where pooled-vault liquidity lives, outside the confidential core.](/img/architecture-deep-dive.svg)

## The accounting block

The accounting block is the system's confidential ledger. The `Accounting` contract on Oasis Sapphire holds an encrypted mapping of deposit addresses to asset balances, and the Privana service maintains it: it processes deposits on each source chain, manages balance changes, screens transactions (KYT), validates balances before trades, reconciles finality after execution, and handles withdrawals across every supported chain.

## The microservices block

The microservices block implements the execution logic for DeFi operations. Privana provides two microservices: swaps and yield (covered in the [Private Swaps](../features/private-swaps.md) and [Idle Yield](../features/idle-yield.md) sections). New microservices like perpetuals or lending can be added without touching the core accounting logic, and each one settles against the same balances and signing path.

## Remote attestation

Attestation reports, signed by Intel's hardware root of trust, allow you to verify remotely that the enclave is running exactly the code it claims to be running. This means you don't need to trust Privana's word about what code is executing. You can cryptographically verify it. Verification reaches beyond the on-chain contracts: the off-chain Privana service and its microservices run in attested ROFL containers, so their swap and yield logic can be verified the same way. This is a stronger guarantee than an audit alone can provide.

## Beyond DeFi

The SDK is composable, so Privana isn't limited to DeFi. Any Web3 product that needs private balances or scoped, time-bounded access can build on the same vaults: a game, for example, can let players hold and spend real assets without signing every action.
