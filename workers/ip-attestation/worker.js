/**
 * Same-origin client-IP attestation for the Transak on-ramp session endpoint.
 *
 * The frontend POSTs the SHA-256 of its signed intent value; the Worker signs
 * the Cloudflare-observed client IP into a short-lived, single-use claim that
 * the ROFL backend verifies with the shared ATTESTATION_SECRET. The raw signed
 * intent never reaches Cloudflare, only its hash.
 *
 * Signed payload (must match src/services/transak.py verify_ip_attestation):
 *   v1|{REFERRER_DOMAIN}|{intent_hash}|{ip}|{iat}|{exp}|{nonce}
 */

const ATTESTATION_TTL_SECONDS = 60;
const INTENT_HASH_PATTERN = /^[0-9a-f]{64}$/;
// The only field is a 64-char hex hash; a well-formed body is well under 256
// bytes. Cap it so this unauthenticated endpoint cannot be made to buffer and
// parse a large payload.
const MAX_BODY_BYTES = 512;

// Reject-heuristics only; the backend re-validates the IP fail-closed.
const REJECTED_V4_PREFIXES = [
  /^10\./,
  /^127\./,
  /^169\.254\./,
  /^172\.(1[6-9]|2\d|3[01])\./,
  /^192\.168\./,
  /^(22[4-9]|2[3-5]\d)\./, // multicast and Class E 224.0.0.0/3
  /^0\./,
  /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./, // CGNAT 100.64.0.0/10
];

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
    },
  });
}

// Read a request body as text without buffering more than `limit` bytes, even
// when Content-Length is absent (chunked/streamed). Throws once the cap is hit.
async function readBoundedText(request, limit) {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel();
      throw new Error("request body too large");
    }
    chunks.push(value);
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(merged);
}

function isRejectedIp(ip) {
  if (!ip) return true;
  if (ip.includes(":")) {
    const lower = ip.toLowerCase();
    // loopback, unspecified, link-local, unique-local, v4-mapped, CF Worker egress
    return (
      lower === "::1" ||
      lower === "::" ||
      lower.startsWith("fe8") ||
      lower.startsWith("fc") ||
      lower.startsWith("fd") ||
      lower.startsWith("::ffff:") ||
      // CF Worker egress 2a06:98c0::/29 — the low three bits of the fourth
      // hextet are zero, so only 2a06:98c0..2a06:98c7 belong to the range.
      /^2a06:98c[0-7](:|$)/.test(lower)
    );
  }
  return REJECTED_V4_PREFIXES.some((pattern) => pattern.test(ip));
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return json(405, { error: "method not allowed" });
    }
    if (!env.ATTESTATION_SECRET || env.ATTESTATION_SECRET.length < 32 || !env.REFERRER_DOMAIN) {
      return json(503, { error: "attestation is not configured" });
    }
    // A cross-zone Worker-originated request carries cf-worker; a direct
    // browser request cannot set it (Cloudflare strips inbound values).
    if (request.headers.get("cf-worker")) {
      return json(403, { error: "indirect requests are not attestable" });
    }
    const ip = request.headers.get("cf-connecting-ip");
    if (isRejectedIp(ip)) {
      return json(400, { error: "client ip is not attestable" });
    }

    // Reject an oversized body before reading it. A declared length over the cap
    // is refused outright; an undeclared/streamed body is bounded while reading.
    const declaredLength = Number(request.headers.get("content-length"));
    if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
      return json(413, { error: "request body too large" });
    }

    let intentHash;
    try {
      const body = await readBoundedText(request, MAX_BODY_BYTES);
      ({ intentHash } = JSON.parse(body));
    } catch {
      return json(400, { error: "invalid request body" });
    }
    if (typeof intentHash !== "string" || !INTENT_HASH_PATTERN.test(intentHash)) {
      return json(400, { error: "invalid intent hash" });
    }

    const iat = Math.floor(Date.now() / 1000);
    const exp = iat + ATTESTATION_TTL_SECONDS;
    const nonceBytes = new Uint8Array(16);
    crypto.getRandomValues(nonceBytes);
    const nonce = [...nonceBytes].map((b) => b.toString(16).padStart(2, "0")).join("");

    const payload = ["v1", env.REFERRER_DOMAIN, intentHash, ip, iat, exp, nonce].join("|");
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(env.ATTESTATION_SECRET),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const sigBytes = new Uint8Array(
      await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload)),
    );
    const sig = [...sigBytes].map((b) => b.toString(16).padStart(2, "0")).join("");

    return json(200, { v: 1, ip, iat, exp, nonce, sig });
  },
};
