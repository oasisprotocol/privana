---
id: fallback-recovery
title: "Fallback & Recovery"
sidebar_position: 3
description: "What happens if the enclave goes offline permanently, or if Privana ceases to exist? The answer is built into the system, not a service promise."
---

What happens if the enclave goes offline permanently, or if Privana ceases to exist? The answer is built into the system, not a service promise.

Your funds are governed by the `Accounting` contract on Oasis Sapphire, not by Privana's off-chain service. Because that state lives on-chain, your ability to exit does not depend on Privana staying online.

## On-chain withdrawal

The `Accounting` contract gives you two withdrawal paths that work even when the off-chain service (the ROFL container) is down:

- **Your balance.** Submit a withdrawal request signed by your wallet. After a short on-chain delay, the contract returns a signed transaction that pays out to your own address on the source chain. Anyone can broadcast it for you.
- **Funds in your deposit address.** If a deposit was never swept into the vault, call the contract directly to recover it. After the same delay, the contract returns a signed transaction that pays out to the destination you chose in that call.

In both paths, the payout address is fixed before the transaction is signed, so even an untrusted broadcaster cannot redirect your funds.

## Beyond a service outage

These on-chain paths cover the common case: the off-chain service is down while Oasis Sapphire stays live. For the harder case of a permanently lost enclave, Privana's recovery model is informed by the **sentinel-wallet design** described in the [Liquefaction research paper](https://arxiv.org/abs/2412.02634): operator-independent recovery that lets asset owners reclaim control without trusting any single party.

:::tip[The important point]

Your assets are not designed to be permanently lockable. Both withdrawal paths are enforced on-chain by Oasis Sapphire: a contract guarantee, not a service-level promise.

:::
