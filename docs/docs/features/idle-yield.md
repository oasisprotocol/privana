---
id: idle-yield
title: "Idle Yield"
sidebar_position: 2
description: "When your assets aren't being swapped, Privana can route idle stablecoin balances to vetted yield protocols, automatically and privately."
---

When your assets aren't being swapped, Privana can route idle stablecoin balances to vetted yield protocols, automatically and privately.

The yield module is **opt-in**. When you activate it for an asset, the Privana yield microservice routes idle balances to whitelisted yield protocols: [Aave](https://aave.com/) at launch, with more to follow. You define which assets participate, the deposit threshold, and the maximum amount. Nothing happens automatically until you enable it.

Your yield accounting (amounts, protocol, position history) is private on Sapphire. The deposit into the protocol itself executes on the external chain from the pooled vault address, the same as any settlement. When you start a swap, the system unwinds any active yield position for that asset first.

## Works with the rest of Privana

Yield is available through the Privana SDK, so an app can offer its users yield on in-app balances without building its own DeFi integration. [Automation Rules](./automation-rules.md) can route realized trading profits into yield automatically.

## Safety model

Privana only routes to protocols that have passed internal vetting. If a protocol is delisted (for example, due to a vulnerability disclosure), the system halts new deposits and begins unwinding existing positions automatically.
