import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    {
      type: 'category',
      label: 'Getting Started',
      description:
        'What Privana is, how it relates to Oasis, and the privacy problem it solves.',
      link: {type: 'generated-index', slug: '/getting-started'},
      items: [
        'overview/introduction',
        'overview/privacy-problem',
        'overview/privana-on-oasis',
      ],
    },
    {
      type: 'category',
      label: 'Core Concepts',
      description:
        'The mental models you need before reading the feature docs: the vault model, TEEs, key encumbrance, and the non-custodial model.',
      link: {type: 'generated-index', slug: '/core-concepts'},
      items: [
        'concepts/privana-overview',
        'concepts/what-is-a-tee',
        'concepts/key-encumbrance',
        'concepts/non-custodial-model',
      ],
    },
    {
      type: 'category',
      label: 'Features',
      description:
        'User-visible capabilities: private swaps, idle yield, automation rules, AI-agent trading, and the Telegram bot.',
      link: {type: 'generated-index', slug: '/features'},
      items: [
        'features/private-swaps',
        'features/idle-yield',
        'features/automation-rules',
        'features/ai-trading-agents',
        'features/telegram-bot',
      ],
    },
    {
      type: 'category',
      label: 'Underlying Technology',
      description:
        "How the stack actually works — Oasis Sapphire, ROFL, the system architecture, the on-chain fallback, the trust model, and the academic research it's all built on.",
      link: {type: 'generated-index', slug: '/underlying-technology'},
      items: [
        'architecture/oasis-sapphire-and-rofl',
        'architecture/deep-dive',
        'architecture/fallback-recovery',
        'architecture/trust-model',
        'architecture/research-basis',
        'architecture/about-us',
        'architecture/integration-partners',
      ],
    },
  ],
};

export default sidebars;
