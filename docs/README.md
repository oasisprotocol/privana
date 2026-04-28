# Privana documentation

Source for [docs.privana.finance](https://docs.privana.finance). Built with [Docusaurus 3](https://docusaurus.io/).

The Markdown content lives under [`docs/`](./docs/). The Docusaurus app shell (config, theme, sidebar) lives in this directory.

## Local development

Requires [Bun](https://bun.sh/) and Node ≥ 20.

```bash
bun install        # install deps from bun.lock
bun start          # dev server with hot reload at http://localhost:3000
```

Other useful scripts:

```bash
bun run build      # production build → ./build/
bun run serve      # serve the production build locally
bun run typecheck  # run tsc against docusaurus.config.ts and sidebars.ts
bun run clear      # clear Docusaurus cache when something is misbehaving
```
