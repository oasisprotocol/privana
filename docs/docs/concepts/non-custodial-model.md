---
id: non-custodial-model
title: "Non-Custodial Model"
sidebar_position: 4
description: "'Non-custodial' is overused in DeFi. Here's what it actually means for Privana, and how it compares to other models."
---

"Non-custodial" is overused in DeFi. Here's what it actually means for Privana, and how it compares to other models.

### Custodial Exchange

- They hold your keys
- They can freeze your funds
- You trust their security
- Can be hacked or go insolvent
- Regulatory seizure risk

### Standard Self-Custody

- You hold the key (seed phrase)
- Must manually sign everything
- Automation = handing over keys
- No privacy: all actions public
- Human error = permanent loss

### Privana (Hardware Protected)

- Key lives in hardware enclave
- Automation via policy — key never exposed
- Trade intents are private by default
- Revoke any delegation instantly
- On-chain fallback recovery built in

The key insight is that **non-custodial doesn't have to mean manual**. Standard wallets force you to choose: either sign every transaction yourself (manual but safe) or give your key to a bot (automated but custodial). Enclave governance breaks this trade-off — you get automation *and* self-custody.

In the pooled vault model, the vault key is managed by Oasis Sapphire smart contracts running inside TEEs. The Privana team operates the servers, but the servers architecturally cannot read or use the vault's private key. This isn't a security promise — it's what Intel SGX's hardware architecture enforces. Privana processes your instructions, but signing happens inside hardware that Privana cannot access.

:::tip[The trust model in plain terms]

You trust that Intel SGX hardware works as designed, and that Oasis Sapphire correctly implements confidential smart contracts. Given those assumptions, no one — not Privana, not the Oasis team, not a server administrator — can access the vault's private key or override your policy rules. The [Trust Model](../architecture/trust-model.md) page details every assumption and its mitigation.

:::
