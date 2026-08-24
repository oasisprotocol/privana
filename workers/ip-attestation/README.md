# On-ramp client-IP attestation Worker

A ~100-line Cloudflare Worker on the frontend zone that signs the
Cloudflare-observed client IP into a short-lived claim. The backend
(`TRANSAK_CLIENT_IP_MODE=attested`) verifies the claim and forwards the IP to
Transak as `x-user-ip`. This uses Transak's documented CDN-derived-IP pattern
(`cf-connecting-ip`) without trusting any spoofable inbound header at the ROFL
proxy.

## Contract

- `POST https://app.testnet.privana.finance/__onramp-ip-attest`
- Request: `{"intentHash": "<sha256 hex of the signed intent value>"}` — the
  raw signed intent never reaches Cloudflare.
- Response: `{"v": 1, "ip", "iat", "exp", "nonce", "sig"}` with a 60-second
  expiry. The SDK passes this object unchanged as `ip_attestation` in
  `POST /onramp/session`.
- Signed payload, shared with `src/services/transak.py`:
  `v1|{REFERRER_DOMAIN}|{intentHash}|{ip}|{iat}|{exp}|{nonce}` (HMAC-SHA256,
  lowercase hex).

## Deploy

```shell
# One-time secret; identical value goes to the backend as the encrypted ROFL
# secret TRANSAK_IP_ATTESTATION_SECRET (>= 32 chars).
wrangler secret put ATTESTATION_SECRET --env staging

wrangler deploy --env staging
```

## Zone preflight (required)

These zone settings change what `cf-connecting-ip` carries; verify them before
enabling attested mode:

- **Pseudo IPv4: Off** — the header must carry the real IPv6, not a mapped
  Class E IPv4 (the Worker and backend both reject `240.0.0.0/4`-style values).
- **"Remove visitor IP headers" managed transform: Off** for this route.
- The route stays on the frontend zone; never proxy or CDN-front the API host,
  because bearer tokens are replayable.

## Limits of the design (known, accepted)

- The `cf-worker` header check rejects honest cross-zone Worker traffic; it is
  a heuristic, not tamper-proof. The backend independently rejects the
  Cloudflare Worker egress range `2a06:98c0::/29`.
- Replay protection is a best-effort in-memory nonce cache in the one-machine
  backend; the primary controls are the 60-second window, the intent binding,
  and the per-user session rate limit.
- A VPN user gets their VPN egress IP attested; that is the same behavior as
  any CDN-derived IP and is acceptable to Transak.
