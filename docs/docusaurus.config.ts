import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Privana',
  tagline: 'Private, non-custodial DeFi on Oasis Sapphire',
  url: 'https://docs.privana.finance',
  baseUrl: '/',

  organizationName: 'oasisprotocol',
  projectName: 'privana',

  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  markdown: {
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },

  themes: ['@docusaurus/theme-mermaid'],

  presets: [
    [
      'classic',
      {
        docs: {
          path: 'docs',
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'light',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Privana',
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          to: '/faq',
          label: 'FAQ',
          position: 'left',
        },
        // Restore when the privana repo is public.
        // {
        //   href: 'https://github.com/oasisprotocol/privana',
        //   label: 'GitHub',
        //   position: 'right',
        // },
      ],
    },
    footer: {
      style: 'light',
      links: [
        {
          title: 'Resources',
          items: [
            {label: 'Liquefaction paper', href: 'https://arxiv.org/abs/2412.02634'},
            {label: 'Oasis Sapphire', href: 'https://docs.oasis.io/build/sapphire/'},
            // Restore when the privana repo is public.
            // {label: 'GitHub', href: 'https://github.com/oasisprotocol/privana'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Oasis Protocol Foundation.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['solidity', 'bash', 'json'],
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
