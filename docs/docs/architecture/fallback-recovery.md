---
id: fallback-recovery
title: "Fallback & Recovery"
sidebar_position: 3
description: "What happens if the enclave goes offline permanently, or if Privana ceases to exist? The answer is built into the system — not a service promise."
---

What happens if the enclave goes offline permanently, or if Privana ceases to exist? The answer is built into the system — not a service promise.

Privana uses a fallback system informed by the sentinel-wallet design described in the Liquefaction research paper:

```js
// The Ethereum-based liveness monitor
Ethereum contract monitors Oasis Sapphire liveness
  → Checks for valid response from enclave every ~24 hours
  → If NO response for 7 days:
      → Challenge period opens (publicly visible on-chain)
      → Backup TEE committee reconstructs vault keys
          from Shamir secret shares (distributed — no single party alone)
      → Keys are released to the ACCESS MANAGER
          (this is you — the vault owner)
      → You can then sweep your assets directly

// No trust in Privana required for recovery
// Recovery process is deterministic and on-chain
```

The fallback is a **smart contract on Ethereum**, not a process Privana controls. It triggers automatically if the enclave becomes unresponsive for a week. A distributed reconstruction process — using Shamir secret shares held by multiple independent parties — allows the vault keys to be recovered by you, without Privana's involvement.

:::tip[The important point]

Your assets are never permanently locked. Even if Privana ceases operations, your funds are recoverable within a week via the on-chain fallback mechanism. This is a **system guarantee**, not a service-level agreement.

:::
