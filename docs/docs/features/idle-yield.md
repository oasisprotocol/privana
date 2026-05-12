---
id: idle-yield
title: "Idle Yield"
sidebar_position: 2
description: "When your assets aren't being swapped, Privana can route idle stablecoin balances to vetted yield protocols — automatically and privately."
---

When your assets aren't being swapped, Privana can route idle stablecoin balances to vetted yield protocols — automatically and privately.

The yield module is **opt-in**. When you activate it for an asset, the Privana yield microservice routes idle balances to whitelisted yield protocols — [Aave](https://aave.com/) at launch, with more protocols planned post-MVP. You define which assets participate, the deposit threshold, and the maximum amount. Nothing happens automatically until you enable it.

Yield positions are managed entirely inside the TEE. Your deposit amounts, protocol choices, and APY history are encrypted — not visible to anyone outside the enclave. When you initiate a swap, the system automatically unwinds any active yield position for that asset before executing the trade.

### Why yield is part of the MVP

Including yield from launch serves three purposes. First, it meets baseline DeFi expectations — users expect idle assets to be productive. Second, it enables **composability across applications**. For example, a gaming application built on the Privana SDK could let its players earn yield on in-game stablecoin holdings without the game developer needing to implement DeFi integrations. Third, it enables automation rules where realized trading profits are automatically routed to yield, compounding returns without your intervention (see [Automation Rules](./automation-rules.md)).

### Safety model

Privana only routes to protocols that have passed internal vetting. The yield module never activates automatically — it's always opt-in. If a protocol is delisted (for example, due to a vulnerability disclosure), the system halts new deposits and begins unwinding existing positions automatically.
