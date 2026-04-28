---
id: trust-model
title: "Trust Model"
sidebar_position: 4
description: "No system is trustless. Privana minimizes trust requirements — but you should understand exactly what assumptions remain."
---

No system is trustless. Privana minimizes trust requirements — but you should understand exactly what assumptions remain.

| Component | Trust Assumption | Mitigation |
| --- | --- | --- |
| Intel SGX | Hardware integrity of the enclave | Industry standard; used by Fireblocks, Azure confidential computing. Side-channel research is ongoing — see [Research Basis](./research-basis.md). |
| Oasis Sapphire | Correct implementation of confidential contracts; blockchain liveness | Open-source, audited. Decentralized validator network. Native rollback protections. [Sapphire docs →](https://docs.oasis.io/dapp/sapphire/) |
| Privana service code | Enclave code does what it claims | Remote attestation allows cryptographic verification of the exact running code. |
| Privana / Oasis team | Cannot access vault keys | Architecture enforces this — SGX prevents privileged access. Not a promise; a hardware constraint. |
| Pooled vault model | Users trust the vault's accounting logic to protect all balances correctly | Accounting logic runs inside TEE on Oasis Sapphire. Trust is bounded by TEE + blockchain security model. |
| Yield protocols | Smart contract risk (Aave at launch) | Only vetted protocols. Yield is opt-in. Automatic unwinding if a protocol is delisted. |
| DEX execution (Seq. 3) | LiFi aggregator executes as expected | Standard on-chain DEX risk. Slippage protection and spread thresholds enforced in policy. |

Two risks Privana cannot eliminate: (1) vulnerabilities in Intel SGX itself — a class of hardware-level attacks that exist for all TEE platforms, and (2) smart contract bugs in the external yield protocols you choose to use. Both are disclosed openly, and the system mitigates them through conservative platform selection, audits, and protocol vetting.

### Complete Knowledge proofs

The Liquefaction framework discusses **Complete Knowledge (CK) proofs** — cryptographic proofs that a user has unencumbered access to a private key. While Privana implements constructive (beneficial) applications of key encumbrance rather than adversarial ones, CK proofs could become a valuable complement — for example, allowing centralized exchanges to verify that a depositor's keys are not encumbered. The Oasis team is monitoring developments in CK proof practicality, particularly as mobile-TEE attestation hardware matures.
