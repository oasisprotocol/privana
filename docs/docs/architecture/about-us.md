---
id: about-us
title: "About Us"
sidebar_position: 6
description: "Who builds Privana, and why."
---

# About Us

Privana is a private corner of DeFi: swap tokens and earn yield without publishing your balance, your trade size, or your timing to everyone with a block explorer.

## Why it exists

Public blockchains are public in a way most people underestimate. Anyone holding your address can read your entire balance sheet, every position you have opened, and the exact second you opened it — permanently, without asking. That is uncomfortable on its own, and it is also expensive: orders that sit in the open before they execute can be traded ahead of.

Most attempts to fix this ask you to trust an operator's promise not to look. Privana doesn't. Your balances are matched, your trades are filled and your yield is accounted for inside a **Trusted Execution Environment** — a sealed region of a processor that encrypts its own memory. Someone with physical access to the machine and full administrator rights reads noise. That includes us.

## Who builds it

Privana is built by the **Oasis Protocol Foundation**, the organisation behind the Oasis Network. It runs on **Oasis Sapphire**, a public blockchain where contract state and transaction data are encrypted by default, and its off-chain services run as **ROFL** applications in attested TEE hardware.

That matters for a reason worth stating plainly: the privacy guarantee does not rest on a promise from a startup. It rests on a public network with independent validators, and on hardware that publishes a signed statement of exactly which code it loaded. You can check rather than trust.

## What we will not claim

**Private is not anonymous.** The fact that you used Privana is visible on a public chain. What you did inside it is not.

**Privana is not a mixer.** It does not obscure where funds came from, and it is not designed to break the link between source and destination.

**Yield comes from somewhere real.** Idle stablecoins earn by being lent in established markets, which means real returns and real protocol risk. The privacy layer does not remove it, and no rate is guaranteed.

## Where things stand

Privana is live on **testnet**. The deposit, swap and earn paths work end to end, and the smart contracts are deployed on Sapphire Testnet. Mainnet is not deployed yet, and the contracts have not completed a third-party security audit.

---

**Next:** [How It Connects](../overview/privana-on-oasis.md) ·
[What is a TEE?](../concepts/what-is-a-tee.md) ·
[Trust Model](./trust-model.md)
