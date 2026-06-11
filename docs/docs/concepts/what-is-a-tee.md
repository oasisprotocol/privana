---
id: what-is-a-tee
title: "What Is a TEE?"
sidebar_position: 2
description: "A Trusted Execution Environment is a secure region of a CPU. Code inside is shielded from the operating system, the server operator, and anyone else."
---

A Trusted Execution Environment is a secure, isolated area inside a computer processor. Code running inside a TEE is protected from everything outside it, including the operating system and the server operator.

Think of it like a locked room inside a building. Work happens inside that room, but even the building's owner can't see what's happening inside or modify it. The room itself enforces the rules.

In Privana's case, the "room" is an Intel SGX enclave on Oasis Sapphire. It holds the vault's private keys, signs transactions, and stores your balances. No one outside the enclave can read it, not even the Oasis team or Privana's engineers.

## What a TEE protects

Vault private keys, user balances, policy rules, transaction history. All encrypted at rest inside the enclave, never exposed to software outside it.

## What a TEE prevents

Reading keys from outside. Overriding user policies. Signing unauthorized transactions. Even an engineer with full server access cannot extract enclave contents.

## What a TEE guarantees

Remote **attestation** lets anyone verify exactly which code is running inside the enclave. If that code is tampered with, its cryptographic measurement changes, so the tampering is detectable.

## Who uses TEEs

Enterprise custody platforms like Fireblocks use TEEs to secure billions in institutional digital assets. Privana brings this same security model to individual DeFi users.

TEEs are built directly into the CPU hardware. Intel's implementation is called **Intel SGX** (Software Guard Extensions). Code inside an SGX enclave runs in a protected region of memory called the Enclave Page Cache (EPC). The processor enforces that no external code (not the OS kernel, not the hypervisor, not the hardware controller) can read or write to EPC memory.

For deeper technical detail on how Oasis Sapphire uses TEEs, see the [Oasis Sapphire documentation](https://docs.oasis.io/build/sapphire/).
