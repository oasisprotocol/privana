---
id: private-swaps
title: "Private Swaps"
sidebar_position: 1
description: "Privana routes every swap through three execution stages — internal matching, vault inventory, and external DEXes — to maximize privacy at each step."
---

When you execute a swap on Privana, your intent goes through up to three execution stages — each optimized for a different balance of privacy, speed, and cost.

Your swap intent (for example, "swap 1.5 ETH for USDC") is sent to the Privana service, which authenticates your request and routes it to the trading microservice running inside the Oasis Sapphire TEE. The microservice then attempts to fill your order through a three-stage pipeline, trying each stage in sequence until the order is filled.

### The three execution sequences

#### 1 — Internal Matching

*Privacy: Full*

Your intent is matched against other active intents within the Privana service. If another user wants the opposite side of your trade, balances are updated in the accounting layer and the trade settles instantly. No external on-chain transaction is generated at all. This is the fastest, cheapest, and most private execution path.

#### 2 — Vault Inventory

*Privacy: Full*

If no internal match is found, your intent is settled against the Oasis-provisioned liquidity held inside the vault. Pricing is sourced from the [LiFi aggregator](https://li.fi/) to ensure alignment with prevailing DeFi market rates. Again, no external transaction — full privacy is maintained.

#### 3 — External DEX Routing

*Privacy: Partial*

If internal liquidity can't fill the order, it's routed through external decentralized exchanges via LiFi (Uniswap, Curve, and others). The trade executes on public-chain mempools, so it's visible on-chain — but it originates from the pooled vault address, not your personal wallet. Individual attribution remains concealed.

:::warning[Privacy varies by execution path]

Sequences 1 and 2 provide strong privacy — no external transaction is generated and your intent stays entirely within the TEE. Sequence 3 routes through public DEX infrastructure, meaning the trade is visible on-chain from the vault address. While no one can trace the trade back to you specifically, the trade itself is publicly visible. Understanding this distinction helps you set realistic expectations.

:::

### When does Sequence 3 activate?

Sequence 3 is used when there isn't enough internal liquidity for the requested pair, when vault inventory is being rebalanced, or during extreme market conditions that exceed the rebalancing engine's capacity. For commonly traded pairs with healthy vault inventory, most trades settle through Sequences 1 or 2.

### How pricing works

In Sequence 2, Privana acts as a counterparty to your trade, settling against its own inventory. Pricing is sourced from the LiFi aggregator to ensure you get fair market rates. The system includes safeguards — price staleness checks and maximum-spread thresholds — to prevent mispricing. While this model isn't immune to brief latency-based discrepancies during volatility, the safeguards are designed to keep pricing fair under normal conditions.

### TWAP execution

For larger orders, Privana will support **Time-Weighted Average Price (TWAP)** execution. Instead of filling the entire order in one transaction — which could move the market — the system splits it into smaller pieces executed over a time window. Each piece is signed and submitted independently, all within the TEE. The splitting logic, timing, and sizes remain private.

### Nonce management

To prevent replay attacks — where someone captures a signed transaction and replays it later — all transactions signed by the enclave use the current account nonce. A transaction inclusion proof is required before the next nonce is unlocked, preventing any malicious actor from re-using a previously signed transaction.
