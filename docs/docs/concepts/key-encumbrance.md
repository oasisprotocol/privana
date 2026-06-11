---
id: key-encumbrance
title: "Key Encumbrance"
sidebar_position: 3
description: "Define wallet rules once and let the hardware enclave enforce them. Key encumbrance gives you DeFi automation without surrendering your private key."
---

Traditional wallets require you to manually approve every transaction. Key encumbrance lets you define rules once, and the hardware enclave enforces them automatically, without ever exposing your keys.

In a standard DeFi wallet, you control a private key and must sign every action yourself. This makes automation impossible without handing your key to a third party, which defeats the purpose of self-custody.

**Key encumbrance** breaks this trade-off. It's a concept from the [Liquefaction research paper](https://arxiv.org/abs/2412.02634) by Austgen et al. at Cornell Tech. The idea: a private key is generated and held inside a TEE, and it is governed by **programmable access-control policies** that define exactly what the key is allowed to sign. The key has never existed outside the enclave. It is "encumbered": it can only act within the bounds of the policies you set.

Instead of signing each transaction yourself, you define a policy once. The enclave then executes on your behalf, but only within those bounds. You can revoke or update the policy at any time.

```js
// Example policy: delegate yield routing, keep direct swap control
{
  asset:            "USDC",
  spend_limit:      1000 USDC/day,    // max automated spend
  destination_lock: ["aave-v3"],      // only this protocol
  expires:          30 days,          // time-bound — auto-expires
  revocable:        true              // you can always cancel
}

// The enclave enforces these limits in hardware.
// No one — including Privana — can execute outside these rules.
```

## Asset-Time Segmentation

Privana enforces a principle called **Asset-Time Segmentation**: at any given time, a specific asset position is exclusively controlled by one actor: either you directly, or a delegation you've granted. Two policies can never simultaneously claim the same assets. This prevents conflicts and ensures you can always exit: a yield delegation cannot prevent you from withdrawing your funds.

## What this changes

Key encumbrance breaks what cryptographers call the **SEAO assumption**: Single-Entity Address-Ownership. With a standard wallet, one address equals one person who controls it absolutely. With key encumbrance, an address can be governed by complex policy rules: multiple delegations, time bounds, asset limits; all enforced in hardware, all without transferring custody. This is what makes automated, private DeFi possible without giving up control.

:::info[A helpful analogy]

Think of key encumbrance like a power of attorney with strict, hardware-enforced limits. You've given instructions, but those instructions are locked into hardware that even the person holding the power of attorney can't change. And you can revoke it the moment you want.

:::
