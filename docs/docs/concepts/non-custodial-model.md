---
id: non-custodial-model
title: "Non-Custodial Model"
sidebar_position: 4
description: "'Non-custodial' is overused in DeFi. Here's what it actually means for Privana, and how it compares to other models."
---

"Non-custodial" is overused in DeFi. Here's what it actually means for Privana, and how it compares to other models.

## Custodial Exchange

- They hold your keys
- They can freeze your funds
- You trust their security
- Can be hacked or go insolvent
- Regulatory seizure risk

## Standard Self-Custody

- You hold the key (seed phrase)
- Must manually sign everything
- Automation = handing over keys
- No privacy: all actions public
- Human error = permanent loss

## Privana (Hardware Protected)

- Key lives in hardware enclave
- Automation via policy — key never exposed
- Trade intents are private by default
- Revoke any delegation instantly
- On-chain fallback recovery built in

The key insight is that **non-custodial doesn't have to mean manual**. Standard wallets force you to choose: either sign every transaction yourself (manual but safe) or give your key to a bot (automated but custodial). Enclave governance breaks this trade-off. You get automation *and* self-custody.

In the pooled vault model, the vault key lives inside Oasis Sapphire's smart contracts, not on Privana's servers. The Privana service relays your instructions, but it never holds the key. Signing happens inside the Sapphire enclave, on hardware the servers can't read. This isn't a policy the team promises to follow; it's what the hardware enforces.

:::tip[The trust model in plain terms]

You trust that Intel SGX hardware works as designed, and that Oasis Sapphire correctly implements confidential smart contracts. Given those assumptions, no one (not Privana, not the Oasis team, not a server administrator) can access the vault's private key or override your policy rules. The [Trust Model](../architecture/trust-model.md) page details every assumption and its mitigation.

:::
