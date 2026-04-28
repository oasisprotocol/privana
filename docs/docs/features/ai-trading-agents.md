---
id: ai-trading-agents
title: "AI Trading Agents"
sidebar_position: 4
description: "Connect AI-powered trading agents to your vault — with strict, hardware-enforced guardrails that ensure the agent can never exceed the limits you set."
---

Connect AI-powered trading agents to your vault — with strict, hardware-enforced guardrails that ensure the agent can never exceed the limits you set.

The rise of AI trading agents has created a serious security problem: many users hand their private keys directly to centralized AI services, sacrificing control and security for automated trading. Key encumbrance offers a fundamentally better approach.

With Privana, you **never expose your private key to an AI agent**. Instead, you grant the agent access through a scoped sub-policy — the agent can trade within the constraints you define, but the signing key remains inside the hardware enclave and is never accessible to the agent itself. Agents connect via scoped API keys; all signing occurs exclusively inside the TEE.

### Agent policy controls

| Policy | What It Controls |
| --- | --- |
| **Daily spending limit** | Maximum USD-equivalent value the agent can deploy per 24-hour window. Resets automatically. Tracked by the TEE. |
| **Asset whitelist** | Explicit list of tokens the agent is permitted to trade. Intents involving non-whitelisted assets are rejected before signing. |
| **Trade direction** | Restrict the agent to buy-only, sell-only, or both. Enables separation of concerns (e.g., a profit-taking agent that can only sell). |
| **Frequency cap** | Maximum trades per hour or per day. Prevents runaway loops or high-frequency strategies that could erode balance through friction costs. |
| **Max single trade size** | Ceiling on the size of any individual trade. Even if the daily limit is high, no single action can exceed the cap. |
| **Yield & withdrawal block** | Agents are structurally prohibited from routing to yield or initiating withdrawals. These actions require direct user authorization and are excluded from agent scope by design. |

:::tip[The difference from other AI trading setups]

On centralized exchanges, AI agents need your API keys — often with withdrawal permissions. On standard DEXes, bots run with your private key exposed in a hot wallet. With Privana, the AI agent proposes actions, and the enclave decides whether they fall within your policy before signing. The agent never touches the key. This is the first time AI-assisted trading is available with genuine non-custodial, hardware-enforced guardrails.

:::
