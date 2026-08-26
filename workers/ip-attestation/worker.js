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
const ATTESTATION_PATH = "/__onramp-ip-attest";
const INTENT_HASH_PATTERN = /^[0-9a-f]{64}$/;
const ATTESTATION_SECRET_PATTERN = /^[\x21-\x7e]{32,}$/;
const REFERRER_DOMAIN_PATTERN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
// The only field is a 64-char hex hash; a well-formed body is well under 256
// bytes. Cap it so this unauthenticated endpoint cannot be made to buffer and
// parse a large payload.
const MAX_BODY_BYTES = 512;

class RequestBodyTooLargeError extends Error {}

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
      try {
        await reader.cancel();
      } finally {
        throw new RequestBodyTooLargeError();
      }
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

function canonicalizeIp(value) {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > 64 ||
    value !== value.trim() ||
    value.includes("%") ||
    value.includes("|")
  ) {
    return null;
  }
  if (!value.includes(":")) {
    const octets = value.split(".");
    if (
      octets.length !== 4 ||
      octets.some(
        (octet) =>
          !/^(0|[1-9][0-9]{0,2})$/.test(octet) || Number(octet) > 255,
      )
    ) {
      return null;
    }
    return octets.join(".");
  }
  try {
    const hostname = new URL(`https://[${value}]/`).hostname;
    if (!hostname.startsWith("[") || !hostname.endsWith("]")) {
      return null;
    }
    return hostname.slice(1, -1).toLowerCase();
  } catch {
    return null;
  }
}

function isValidReferrerDomain(value) {
  if (typeof value !== "string" || !REFERRER_DOMAIN_PATTERN.test(value)) {
    return false;
  }
  const topLevelLabel = value.slice(value.lastIndexOf(".") + 1);
  return /[a-z]/.test(topLevelLabel);
}

function isRejectedIp(ip) {
  if (ip.includes(":")) {
    const lower = ip.toLowerCase();
    // Unspecified/compatible, link/site-local, unique-local, multicast, mapped,
    // and CF Worker egress. The backend independently enforces public unicast.
    return (
      lower.startsWith("::") ||
      lower.startsWith("fc") ||
      lower.startsWith("fd") ||
      lower.startsWith("fe") ||
      lower.startsWith("ff") ||
      // CF Worker egress 2a06:98c0::/29 — the low three bits of the second
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
    if (
      typeof env.ATTESTATION_SECRET !== "string" ||
      !ATTESTATION_SECRET_PATTERN.test(env.ATTESTATION_SECRET) ||
      !isValidReferrerDomain(env.REFERRER_DOMAIN)
    ) {
      return json(503, { error: "attestation is not configured" });
    }
    let requestUrl;
    try {
      requestUrl = new URL(request.url);
    } catch {
      return json(400, { error: "invalid request url" });
    }
    if (
      requestUrl.protocol !== "https:" ||
      requestUrl.hostname !== env.REFERRER_DOMAIN ||
      requestUrl.port ||
      requestUrl.pathname !== ATTESTATION_PATH ||
      requestUrl.search
    ) {
      return json(403, { error: "request host is not attestable" });
    }
    // Worker-originated subrequests carry cf-worker. Reject its presence as an
    // additional heuristic; the backend independently rejects Worker egress.
    if (request.headers.get("cf-worker")) {
      return json(403, { error: "indirect requests are not attestable" });
    }
    const ip = canonicalizeIp(request.headers.get("cf-connecting-ip"));
    if (!ip || isRejectedIp(ip)) {
      return json(400, { error: "client ip is not attestable" });
    }

    const contentType = request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
    if (contentType !== "application/json") {
      return json(415, { error: "content type must be application/json" });
    }

    // Reject an oversized body before reading it. A declared length over the cap
    // is refused outright; an undeclared/streamed body is bounded while reading.
    const declaredLength = Number(request.headers.get("content-length"));
    if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
      return json(413, { error: "request body too large" });
    }

    let body;
    try {
      body = JSON.parse(await readBoundedText(request, MAX_BODY_BYTES));
    } catch (error) {
      if (error instanceof RequestBodyTooLargeError) {
        return json(413, { error: "request body too large" });
      }
      return json(400, { error: "invalid request body" });
    }
    if (
      typeof body !== "object" ||
      body === null ||
      Array.isArray(body) ||
      Object.keys(body).length !== 1 ||
      !Object.hasOwn(body, "intentHash")
    ) {
      return json(400, { error: "invalid request body" });
    }
    const { intentHash } = body;
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
