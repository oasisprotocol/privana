---
id: privana-on-oasis
title: "Privana on Oasis"
sidebar_position: 3
sidebar_label: "How It Connects"
description: "Privana runs on Oasis Sapphire, a confidential blockchain. Here's how the two layers fit together."
---

Privana runs on Oasis Sapphire, a confidential blockchain. Here's how the two layers fit together.

Each layer has a distinct role. The privacy and security guarantees come from Oasis Sapphire's confidential contracts, not from the external chains, which only ever see completed, signed transactions.

![How Privana connects to Oasis: Privana's app and backend service build on Oasis Sapphire, whose confidential contracts hold the keys and sign the transactions that settle on external chains (Ethereum, Base, and HyperEVM). Those chains see ordinary transactions from the pooled vault, not your personal wallet.](/img/privana-on-oasis.svg)

**Oasis Sapphire** is a blockchain built by the [Oasis](https://oasis.net/) team. What makes it special is that every smart contract on Sapphire runs inside a hardware enclave (a TEE). Contract storage is encrypted. Even the validators running the network can't read what's inside a contract. This is the confidential foundation everything else is built on. [Learn more about Oasis Sapphire →](https://docs.oasis.io/build/sapphire/)

**Privana** is the consumer-facing DeFi application built on Oasis Sapphire by the Oasis team. It's where you actually interact: connect your wallet, deposit assets, execute swaps, set up automation rules, and manage your portfolio.

:::tip[Why this matters]

The privacy and security guarantees you get in Privana aren't implemented by Privana alone. They come from Oasis Sapphire's confidential smart contracts. This means the guarantees are enforced at the hardware and blockchain level, not just at the application level.

:::
