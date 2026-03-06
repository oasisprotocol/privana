import * as bech32 from "bech32";

/**
 * Parse a ROFL app ID from either hex (0x-prefixed) or bech32 (rofl1...) format.
 * Returns the 21-byte hex string with 0x prefix.
 */
export function parseRoflAppId(input: string): string {
  // If already hex format, validate and return
  if (input.startsWith("0x")) {
    const hexBytes = input.slice(2);
    if (hexBytes.length !== 42) {
      throw new Error(`Invalid ROFL app ID: expected 21 bytes (42 hex chars), got ${hexBytes.length / 2} bytes`);
    }
    if (!/^[0-9a-fA-F]+$/.test(hexBytes)) {
      throw new Error("Invalid ROFL app ID: contains non-hex characters");
    }
    return input.toLowerCase();
  }

  // Try bech32 decode for rofl1... format
  if (input.startsWith("rofl1")) {
    try {
      const decoded = bech32.decode(input);
      if (decoded.prefix !== "rofl") {
        throw new Error(`Invalid ROFL app ID prefix: expected 'rofl', got '${decoded.prefix}'`);
      }
      const bytes = bech32.fromWords(decoded.words);
      if (bytes.length !== 21) {
        throw new Error(`Invalid ROFL app ID: expected 21 bytes, got ${bytes.length} bytes`);
      }
      return "0x" + Buffer.from(bytes).toString("hex");
    } catch (e) {
      if (e instanceof Error && e.message.includes("Invalid ROFL")) {
        throw e;
      }
      throw new Error(`Invalid bech32 ROFL app ID: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  throw new Error("Invalid ROFL app ID format: must be hex (0x-prefixed) or bech32 (rofl1...)");
}
