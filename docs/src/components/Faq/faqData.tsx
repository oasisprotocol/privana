import React, {type ReactNode} from 'react';
import Link from '@docusaurus/Link';

/**
 * Source of truth for the /faq page.
 *
 * Most answers are plain strings (wrapped in a <p> by the renderer). An answer
 * that needs richer markup (a list, an inline link) is authored as JSX.
 */

export type FaqItem = {
  q: string;
  a: ReactNode;
};

export type FaqCategory = {
  id: string; // anchor slug + scroll-spy key
  label: string;
  items: FaqItem[];
};

export const FAQ_CATEGORIES: FaqCategory[] = [
  {
    id: 'overview',
    label: 'Overview',
    items: [
      {
        q: 'What is Privana?',
        a: 'Privana is a private, non-custodial DeFi application for swapping tokens, earning yield on stablecoins, and automating trading strategies. All activity executes inside hardware-secured enclaves (TEEs) on Oasis Sapphire, a confidential blockchain. Transaction intent, balances, and trading history are private by default. Your keys never leave the hardware.',
      },
      {
        q: 'How is Privana different from a standard DEX?',
        a: 'On a standard DEX like Uniswap, swaps enter a public mempool before settling, where bots running MEV strategies front-run them for a worse price. Privana routes swaps through a TEE, signing and submitting transactions directly from inside the enclave with no mempool exposure. Trades that do route externally originate from a shared vault address, not your personal wallet.',
      },
      {
        q: 'How does Privana compare to a centralized exchange?',
        a: 'Custodial exchanges hold your private keys and can freeze funds, be hacked, or become insolvent. Privana keys are held inside a hardware enclave that the team cannot access, automation runs via programmable policies enforced in hardware, and trading activity is private by default.',
      },
    ],
  },
  {
    id: 'privacy-security',
    label: 'Privacy & Security',
    items: [
      {
        q: 'Why are swaps on Privana private?',
        a: 'Privana processes every swap inside a TEE, a hardware-isolated enclave that no one outside can read. Transactions are signed and submitted directly from inside the enclave, bypassing the public mempool. All users share a single pooled vault address per chain, so external observers cannot attribute any individual trade to a specific wallet.',
      },
      {
        q: 'Does sourcing liquidity from other chains expose my trades?',
        a: 'No. Routing decisions happen inside the TEE before anything touches external infrastructure. When Privana sources liquidity from another chain, it does so from the shared vault address, not yours. Your wallet, your trade size, and your target are never visible to the external chain.',
      },
      {
        q: "Can Privana be verified? How do I know it's doing what it says?",
        a: "Oasis ROFL provides cryptographic attestation that the TEE is running the published code. The integrity of the enclave is verifiable independently. If the code running inside differed from what's published, attestation would fail.",
      },
      {
        q: 'Can Privana access my funds? What does the Privana team have access to?',
        a: 'No. Private keys are generated inside the TEE and have never existed outside it. Privana engineers cannot read them. Automation rules are enforced in hardware, not by policy, so no one at Privana can override them.',
      },
      {
        q: 'What guarantees do I have that I can always access my funds?',
        a: "A smart contract on Ethereum monitors system liveness every 24 hours. If the enclave is unresponsive for seven days, a challenge period opens onchain and vault keys are reconstructed from distributed secret shares held by independent parties. The keys are released directly to you and you can sweep your funds without Privana's involvement. This is an onchain guarantee, not a service-level promise.",
      },
    ],
  },
  {
    id: 'swapping-yield',
    label: 'Swapping & Yield',
    items: [
      {
        q: 'What can I deposit, and what can I swap?',
        a: (
          <>
            <p>
              <strong>Deposit.</strong> Privana runs one vault per chain. Each vault holds two assets: a stablecoin and the chain's native token.
            </p>
            <table>
              <thead>
                <tr>
                  <th>Chain</th>
                  <th>Stablecoin</th>
                  <th>Native token</th>
                </tr>
              </thead>
              <tbody>
                <tr><td>Ethereum</td><td>USDC</td><td>ETH</td></tr>
                <tr><td>Base</td><td>USDC</td><td>ETH</td></tr>
                <tr><td>HyperEVM</td><td>USDC</td><td>HYPE</td></tr>
              </tbody>
            </table>
            <p>
              <strong>Swap.</strong> Not limited to those six. When both sides of your trade are assets Privana already holds, the swap settles against vault liquidity. Everything else routes through LiFi aggregator, which reaches most of DeFi. Those trades execute from a pooled vault address, so they appear on-chain but can't be traced back to you.
            </p>
          </>
        ),
      },
      {
        q: 'How does Privana execute a swap?',
        a: (
          <>
            <p>
              Privana routes swaps through a three-stage pipeline, trying each
              stage in sequence until the order is filled:
            </p>
            <ul>
              <li>
                <strong>Stage 1: Internal matching.</strong> Your intent is
                matched against other active intents inside the vault. If
                matched, balances update instantly with no external transaction.
              </li>
              <li>
                <strong>Stage 2: Vault inventory.</strong> If no internal match
                is found, the trade settles against vault inventory via the LiFi
                aggregator. Still no external transaction.
              </li>
              <li>
                <strong>Stage 3: External DEX routing.</strong> If internal
                liquidity is insufficient, the trade routes through external
                DEXes. Visible onchain, but originates from the shared vault
                address, not your wallet.
              </li>
            </ul>
          </>
        ),
      },
      {
        q: 'How does idle yield work on Privana?',
        a: "Privana's yield module routes idle stablecoin balances to vetted yield protocols automatically when you activate it. The module is opt-in: you define which assets participate, deposit thresholds, and maximum amounts. When you initiate a swap, any active yield position for that asset unwinds automatically before execution.",
      },
      {
        q: 'Can I withdraw at any time?',
        a: 'Privana maintains an instant liquidity reserve so withdrawals are not subject to lock-up periods.',
      },
      {
        q: 'What are the fees?',
        a: 'Early waitlist members trade fee-free for 90 days. Full fee schedule will be published at launch.',
      },
    ],
  },
  {
    id: 'agents-automation',
    label: 'Agents & Automation',
    items: [
      {
        q: 'What trading rules can I automate on Privana?',
        a: 'Privana supports seven automation rule types at launch: trailing stop-loss, profit-take, price-triggered rebalancing, time-scheduled execution (DCA), Nightguard (overnight rebalance to stablecoins), profit-to-yield routing, and yield-to-trading. All rules are enforced in hardware with no manual signing required.',
      },
      {
        q: 'Do I need to stay online for automation rules to run?',
        a: 'No. Once configured and funded, the enclave executes rules continuously without requiring your presence.',
      },
      {
        q: 'Can I connect an AI trading agent to Privana? What limits apply to AI agents?',
        a: 'Yes, via a scoped sub-policy. The agent trades within limits you define: a daily spending limit, asset whitelist, trade direction, frequency cap, and maximum single trade size. Your private key stays inside the TEE and is never accessible to the agent. AI agents cannot initiate withdrawals or route funds to yield — those actions require direct user authorization.',
      },
    ],
  },
  {
    id: 'self-custody',
    label: 'Self-Custody',
    items: [
      {
        q: 'Do I need a wallet to use Privana?',
        a: 'No. Sign in with an email and a wallet is generated for you on the spot through Turnkey. No extension necessary, no seed phrase to write down. If you already have a wallet, you can use that instead. Either way, the wallet remains fully non-custodial.',
      },
      {
        q: 'Can I fund my account with a card?',
        a: 'Yes, through Transak. Pick an amount, pay, and the assets arrive in your vault. Transak is a regulated on-ramp and handles identity verification directly — that verification is between you and Transak.',
      },
      {
        q: 'Who holds my private keys on Privana?',
        a: "Private keys on Privana are held inside the TEE on Oasis Sapphire. Privana does not hold your keys and engineers cannot access them. The vault's signing key is generated inside the enclave and has never existed outside it, enforced by Intel SGX hardware, not policy.",
      },
      {
        q: 'How does my vault interact with multiple chains?',
        a: 'Your vault on Oasis Sapphire holds signing authority for each supported chain. The enclave derives a chain-specific key for each network and signs transactions directly on your behalf. Nothing moves through a bridge. The TEE acts as the signer across chains using keys that never leave the hardware.',
      },
      {
        q: 'What is key encumbrance?',
        a: (
          <p>
            Key encumbrance is a cryptographic mechanism that allows a private
            key inside a TEE to sign transactions automatically, but only within
            programmable rules you define: specific assets, actions, and
            conditions. You can update or revoke those rules at any time. This
            makes non-custodial automation possible without handing your key to
            a third party. The concept originates from the Liquefaction research
            paper by Austgen et al. at Cornell Tech (
            <Link to="https://arxiv.org/abs/2412.02634">arXiv: 2412.02634</Link>,
            2024).
          </p>
        ),
      },
      {
        q: 'What if I lose access to my account?',
        a: (
          <p>
            Your account is your wallet, and your funds are governed on-chain by
            a smart contract on Oasis Sapphire, not by Privana. As long as you
            control your wallet, you can withdraw your funds directly on-chain at
            any time, even if Privana is offline, through the paths described in{' '}
            <Link to="/architecture/fallback-recovery">Fallback &amp; Recovery</Link>.
            Privana never holds your keys, so keep your wallet&apos;s recovery
            phrase safe.
          </p>
        ),
      },
    ],
  },
];
