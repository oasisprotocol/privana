---
id: telegram-bot
title: "Telegram Bot"
sidebar_position: 5
description: "Manage your vault, execute swaps, and receive real-time notifications from Telegram — with privacy-preserving architecture that keeps your identity hidden."
---

Manage your vault, execute swaps, and receive real-time notifications from Telegram — with privacy-preserving architecture that keeps your identity hidden.

The Privana Telegram bot lets you interact with your vault directly from Telegram: check balances, execute swaps, manage automation rules, and receive real-time notifications about your portfolio. It operates through the Privana TEE, meaning your commands are processed inside the enclave with the same privacy guarantees as the main interface.

### Privacy-preserving connection

Unlike typical DeFi notification bots that require an email address or wallet address (permanently linking your identity to on-chain activity), the Privana bot uses a **privacy-preserving connection**. Your Telegram handle is never stored in plaintext on any server. The connection is established through a link generated inside the enclave, and the TEE sends messages directly to the Telegram API without passing your handle to any Oasis-controlled server process.

### What the bot can do

| Category | Capabilities |
| --- | --- |
| **Trading** | Execute swaps, confirm or decline profit-take opportunities, receive stop-loss trigger alerts with intervention windows |
| **Portfolio** | View vault summary (balances, active rules, recent trades), set custom price-alert thresholds |
| **Automation** | Enable or disable Nightguard, pause or resume active automation rules, route captured profits to yield |
| **Notifications** | Trade execution confirmations, TWAP progress updates, yield activity alerts, AI-agent trade summaries, policy-blocked trade alerts |

:::info[Security by design]

The bot cannot modify vault policies or guardrails, cannot access or modify API keys, and cannot add or remove AI-agent connections. High-risk actions like policy changes require the full Privana interface. This limited action scope is a deliberate security decision — the bot is designed so that even if a Telegram account were compromised, the attacker's ability to cause damage is structurally bounded.

:::
