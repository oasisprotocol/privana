import { Buffer } from "node:buffer";

const chunks = [];
for await (const chunk of process.stdin) {
  chunks.push(chunk);
}
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));

if (input.nowMs !== undefined) {
  Date.now = () => input.nowMs;
}

if (input.nonceHex !== undefined) {
  const nonce = Buffer.from(input.nonceHex, "hex");
  const runtimeCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {
      subtle: runtimeCrypto.subtle,
      getRandomValues(target) {
        if (!(target instanceof Uint8Array) || target.byteLength !== nonce.byteLength) {
          throw new Error("unexpected nonce target");
        }
        target.set(nonce);
        return target;
      },
    },
  });
}

const workerModule = await import("../../workers/ip-attestation/worker.js");

const body = input.bodyText ?? (input.body === undefined ? undefined : JSON.stringify(input.body));
const response = await workerModule.default.fetch(
  new Request(input.url, {
    method: input.method ?? "POST",
    headers: input.headers ?? {},
    body,
  }),
  input.env ?? {},
);
const responseText = await response.text();
let responseBody;
try {
  responseBody = JSON.parse(responseText);
} catch {
  responseBody = responseText;
}

process.stdout.write(
  JSON.stringify({
    status: response.status,
    headers: Object.fromEntries(response.headers.entries()),
    body: responseBody,
  }),
);
