---
id: private-swaps
title: "Private Swaps"
sidebar_position: 1
description: "Privana routes every swap through three execution stages (internal matching, vault inventory, and external DEXes) to maximize privacy at each step."
---

When you execute a swap on Privana, your intent goes through up to three execution stages, each optimized for a different balance of privacy, speed, and cost.

Your swap intent (for example, "swap 1.5 ETH for USDC") is sent to the Privana service, which authenticates your request and routes it to the swap microservice running inside a ROFL TEE container. The microservice then attempts to fill your order through a three-stage pipeline, trying each stage in sequence until the order is filled.

## The three execution stages

### 1 — Internal Matching

Your intent is matched against other active intents within the Privana service. If another user wants the opposite side of your trade, balances are updated in the accounting layer and the trade settles instantly. Nothing settles on an external chain, so it's the fastest and cheapest path.

### 2 — Vault Inventory

If no internal match is found, your intent is settled against the Oasis-provisioned liquidity held inside the vault. Pricing is sourced from the [LiFi aggregator](https://li.fi/) to ensure alignment with prevailing DeFi market rates. Again, no external transaction, so full privacy is maintained.

### 3 — External DEX Routing

If internal liquidity can't fill the order, it's routed through external decentralized exchanges via LiFi (Uniswap, Curve, and others). The trade executes on public-chain mempools, so it's visible on-chain, but it originates from the pooled vault address, not your personal wallet. Individual attribution remains concealed.

:::warning[Privacy varies by execution path]

Stages 1 and 2 provide strong privacy: no external transaction is generated and your intent stays entirely within the TEE. Stage 3 routes through public DEX infrastructure, meaning the trade is visible on-chain from the vault address. While no one can trace the trade back to you specifically, the trade itself is publicly visible.

:::

## When does Stage 3 activate?

Stage 3 is used when there isn't enough internal liquidity for the requested pair, when vault inventory is being rebalanced, or during extreme market conditions that exceed the rebalancing engine's capacity. For commonly traded pairs with healthy vault inventory, most trades settle through Stages 1 or 2.

## How pricing works

In Stage 2, Privana acts as a counterparty to your trade, settling against its own inventory. Pricing is sourced from the LiFi aggregator to ensure you get fair market rates. The system includes safeguards (price staleness checks and maximum-spread thresholds) to prevent mispricing.

## TWAP execution

For larger orders, Privana will support **Time-Weighted Average Price (TWAP)** execution. Instead of filling the entire order in one transaction (which could move the market), the system splits it into smaller pieces executed over a time window. Each piece is signed inside the enclave, then broadcast publicly from the pooled vault address. The splitting logic, timing, and sizes stay private inside the TEE.

## Replay protection

Every transaction the enclave signs uses the account's current nonce, and each internal operation carries its own signed nonce. Once a nonce is spent it can't be reused, so a captured signature can't be replayed.
