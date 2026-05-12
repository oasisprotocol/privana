---
id: automation-rules
title: "Automation Rules"
sidebar_position: 3
description: "Define programmable rules that execute trades automatically when your conditions are met — without exposing your keys or requiring you to be online."
---

Privana lets you define programmable rules that execute trades automatically when conditions are met — without exposing your keys or requiring your presence.

The automation engine is built on the same [key encumbrance](../concepts/key-encumbrance.md) model that powers everything in Privana. Each rule you create is stored as an **access-control policy** inside the enclave. When the trigger condition is met, the enclave executes the action on your behalf — using the vault's encumbered key, within the bounds you've defined. No external service can see your rules or trigger conditions.

This gives you a capability that has not previously existed in self-custodial DeFi: **automated, policy-bounded trade execution** — like the conditional-order features of centralized exchanges, but without surrendering custody of your assets.

### Available rule types

| Rule | Trigger | What It Does |
| --- | --- | --- |
| **Trailing Stop-Loss** | Portfolio position declines by a percentage you set | Rebalances to a predefined allocation (e.g., full conversion to USDC). Includes a 5-minute confirmation window to filter short-lived volatility. |
| **Profit-Take** | Position appreciates to your target percentage | Rebalances into stablecoins. Supports stacking multiple thresholds at different price levels. |
| **Price-Triggered** | Market price of an asset crosses your threshold | Executes a predefined rebalance. Not tied to your portfolio P&L — monitors absolute market price. |
| **Time-Scheduled** | Fixed time or interval (daily, weekly, monthly) | Executes a rebalance or allocation change. Ideal for dollar-cost averaging or periodic maintenance. |
| **Nightguard** | Position declines within a 12-hour activation window | Rebalances to stablecoins within the window, then automatically deactivates. Designed for overnight risk management. |
| **Profit-to-Yield** | Realized trading profit exceeds your threshold | Routes profits to the yield microservice (e.g., Aave). Principal remains untouched — only realized gains are redirected. |
| **Yield-to-Trading** | Accumulated yield exceeds your threshold | Deploys yield into spot positions via the trading microservice. Threshold-gated to prevent excessive churn. |

Rules are **non-conflicting by construction**. The accounting layer enforces balance constraints that prevent two active rules from claiming the same assets simultaneously, consistent with the asset-time segmentation principle that underpins all Privana policies.
