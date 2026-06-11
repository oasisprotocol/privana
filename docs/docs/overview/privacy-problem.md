---
id: privacy-problem
title: "The Privacy Problem"
sidebar_position: 2
description: "Every public DEX exposes your intent before your trade executes. Privana exists to change that."
---

Every public DEX exposes your intent before your trade executes. Privana exists to change that.

When you submit a swap on Uniswap, 1inch, or any standard decentralized exchange, your transaction enters a public waiting area called the **mempool** before it's included in a block. During that window, anyone watching the network can see exactly what you're about to do, and act on it.

Sophisticated bots run by trading firms and MEV searchers watch this queue constantly. They insert their own transactions before yours, extract value, and let your transaction go through at a worse price. This is called **Maximal Extractable Value (MEV)**, and it has cost DeFi users over $1 billion since 2020.

Private RPCs and flashbots relays reduce this exposure, but they shift trust to a relay operator who can still see your intent. They help, but they're patches on a structural problem.

:::warning[The second problem: your on-chain history]

Beyond front-running, every swap you've ever made is permanently visible on-chain. Your wallet address is a public ledger of your financial activity. Anyone can track your patterns, your positions, and your timing. Privana addresses both the front-running problem and the history problem structurally, not with workarounds.

:::
