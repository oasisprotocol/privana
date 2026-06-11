---
id: privana-overview
title: "Privana Overview"
sidebar_position: 1
description: "Privana operates pooled, policy-governed vaults that hold assets inside hardware enclaves and execute DeFi actions privately."
---

Privana operates pooled, policy-governed vaults that hold assets inside hardware enclaves and execute DeFi actions privately.

## The pooled vault model

When you deposit into Privana, your assets enter a **vault**: a smart contract on Oasis Sapphire that operates inside an Intel SGX enclave. But unlike a traditional wallet where each user has their own private key, Privana uses a **pooled model**: a single vault per chain holds assets for multiple users.

Your individual balance is tracked by a **confidential accounting layer**: a set of encrypted smart contracts that maintain a private mapping of who owns what. Think of it like a bank vault where everyone's gold is stored together, but the ledger tracking individual ownership is locked in a safe that only the system can read.

This pooling is a deliberate design choice with three benefits:

**Privacy amplification.** When a trade executes from a Privana vault, external observers see a transaction from one large vault address, not from your personal wallet. Since all user activity originates from the same address, no one can attribute a specific trade to a specific person.

**Liquidity efficiency.** The Oasis team seeds the vaults with inventory of commonly traded assets. When your trade can be matched against this internal liquidity, it settles instantly inside the vault, with no external transaction at all. That's faster and cheaper.

**Lower costs.** The pooled model eliminates the need to deploy individual smart contracts for each user, keeping gas costs and complexity low.

## What the accounting layer tracks

### Deposit Processing

Verifies your deposit on the source chain and registers it to your balance in the confidential ledger.

### Balance Validation

Checks that you have sufficient funds before any trade or yield action is processed.

### Transaction Screening

Runs Know Your Transaction (KYT) checks on incoming deposits to filter funds from sanctioned sources: a compliance layer that doesn't compromise intra-vault privacy.

### Finality Reconciliation

After a trade executes, updates all affected balances across vaults to reflect the new state.

:::info[Privana's vaults are chain-agnostic]

Because the vault's key lives inside a TEE and all logic is enforced off-chain from the target blockchain's perspective, the same model extends to *any* chain whose transactions can be signed with a private key, including chains with no native smart contract support, like Bitcoin. The target chain sees only ordinary signed transactions; the programmability is invisible to it. The Liquefaction research paper calls this capability "overlay smart contracts." [Read the paper →](https://arxiv.org/abs/2412.02634)

:::
