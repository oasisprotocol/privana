# On-ramp client-IP attestation Worker

A small Cloudflare Worker on the frontend zone that signs the
Cloudflare-observed client IP into a short-lived claim. The backend
(`TRANSAK_CLIENT_IP_MODE=attested`) verifies the claim and forwards the IP to
Transak as `x-user-ip`. This uses Transak's documented CDN-derived-IP pattern
(`cf-connecting-ip`) without trusting any spoofable inbound header at the ROFL
proxy.

## HTTP contract

The browser SDK calls this same-origin Worker route directly; the Privana
backend does not serve it.

- `POST https://app.privana.finance/__onramp-ip-attest` (production) or
  `POST https://app.testnet.privana.finance/__onramp-ip-attest` (staging)
- Request: `{"intentHash": "<sha256 hex of the signed intent value>"}` — the
  raw signed intent never reaches Cloudflare.
- Response: `{"v": 1, "ip", "iat", "exp", "nonce", "sig"}` with a 60-second
  expiry. The SDK must fetch a new claim before each `POST /onramp/session` and
  pass it unchanged as `ip_attestation`.
- Signed payload, shared with `src/services/transak.py`:
  `v1|{REFERRER_DOMAIN}|{intentHash}|{ip}|{iat}|{exp}|{nonce}` (HMAC-SHA256,
  lowercase hex).

## Deploy

```shell
# One-time secret per environment; identical value goes to the matching ROFL
# deployment (staging: testnet, production: mainnet) as the encrypted secret
# TRANSAK_IP_ATTESTATION_SECRET (>= 32 chars). Never reuse one across both.
wrangler secret put ATTESTATION_SECRET --env staging
wrangler deploy --env staging

wrangler secret put ATTESTATION_SECRET --env production
wrangler deploy --env production
```

## Zone preflight (required)

These zone settings change what `cf-connecting-ip` carries; verify them before
enabling attested mode:

- **Pseudo IPv4: Off** — the header must carry the real IPv6, not a mapped
  Class E IPv4 (the Worker and backend both reject `240.0.0.0/4`-style values).
- **"Remove visitor IP headers" managed transform: Off** for this route.
- The route stays on the frontend zone; never proxy or CDN-front the API host,
  because bearer tokens are replayable.

### Edge rate-limit gate

Create a Cloudflare zone
[rate limiting rule](https://developers.cloudflare.com/waf/rate-limiting-rules/)
before deploying the Worker, then verify it before enabling attested mode:

- expression: `http.request.uri.path eq "/__onramp-ip-attest"`;
- counting characteristic: source IP (`ip.src`), or **IP with NAT support** if
  the zone plan supports it;
- initial limit: 5 requests per 10 seconds per characteristic;
- action: block for 10 seconds.

When supported, also match the frontend host and `POST` method. On a zone plan
with a single rate limiting rule, match both frontend hosts in that one rule
(`http.host in {"app.privana.finance" "app.testnet.privana.finance"}`). After
deployment, verify mitigation in Cloudflare Security Events and normal access
after the block window. The gate must run before Worker execution; a
Worker-local counter is not a substitute.

## Security notes

- The `cf-worker` header check rejects honest cross-zone Worker traffic; it is
  a heuristic, not tamper-proof. The backend independently rejects the
  Cloudflare Worker egress range `2a06:98c0::/29`.
- Replay protection is a best-effort in-memory nonce cache in the one-machine
  backend; the primary controls are the 60-second window, the intent binding,
  and the per-user session rate limit.
