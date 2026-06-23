import { artifacts } from "hardhat";
import { promises as fs } from "node:fs";
import * as path from "node:path";

// Per-chain contract-size limits enforced by each deploy target. Sapphire
// allows 64 KiB; Base and other standard EVM chains stay at the EIP-170
// 24576-byte cap.
const SAPPHIRE_LIMIT_BYTES = 65536; // 64 KiB
const EIP170_LIMIT_BYTES = 24576; // standard EVM (Base, etc.)

// Safety buffer held below each contract's real limit so nothing is ever
// deployed right at the hard ceiling. The enforced cap is `limit - buffer`.
const SIZE_BUFFER_BYTES = 1024; // 1 KiB

// Per-contract real limits; `required: true` makes a missing artifact a hard
// failure (a rename/build regression), not a silent skip.
const CONTRACTS: ReadonlyArray<{
  name: string;
  limit: number;
  required: boolean;
}> = [
  { name: "Accounting", limit: SAPPHIRE_LIMIT_BYTES, required: true },
  // Base-side (base-sepolia, chainId 84532): standard EIP-170, NOT raised.
  // Optional — skipped silently until their artifacts exist.
  { name: "XRose", limit: EIP170_LIMIT_BYTES, required: false },
  { name: "ROFLBridge", limit: EIP170_LIMIT_BYTES, required: false },
];

const BASELINE_PATH = path.join(__dirname, ".bytecode-baseline.json");

type BaselineFile = {
  version: number;
  contracts: Record<string, { size: number }>;
  headroomFloor?: Record<string, number>;
};

type CheckResult = {
  name: string;
  size: number | null; // null means artifact missing
  cap: number;
  baselineSize: number | null; // null means no baseline entry
  headroomFloor: number | null;
  required: boolean; // when true, missing artifact is a hard failure
};

async function readSize(contractName: string): Promise<number | null> {
  try {
    const artifact = await artifacts.readArtifact(contractName);
    const bytecode = artifact.deployedBytecode.startsWith("0x")
      ? artifact.deployedBytecode.slice(2)
      : artifact.deployedBytecode;
    return bytecode.length / 2;
  } catch {
    return null;
  }
}

async function loadBaseline(): Promise<BaselineFile | null> {
  try {
    const raw = await fs.readFile(BASELINE_PATH, "utf8");
    return JSON.parse(raw) as BaselineFile;
  } catch {
    return null;
  }
}

async function writeBaseline(b: BaselineFile): Promise<void> {
  await fs.writeFile(BASELINE_PATH, JSON.stringify(b, null, 2) + "\n");
}

async function check(
  name: string,
  cap: number,
  baseline: BaselineFile | null,
  required: boolean,
): Promise<CheckResult> {
  const size = await readSize(name);
  const baselineSize = baseline?.contracts?.[name]?.size ?? null;
  const headroomFloor = baseline?.headroomFloor?.[name] ?? null;
  return { name, size, cap, baselineSize, headroomFloor, required };
}

function format(r: CheckResult): string {
  if (r.size === null) {
    return `${r.name}: (skipped: artifact missing)`;
  }
  const headroom = r.cap - r.size;
  const head = `${r.name}: ${r.size} / ${r.cap} bytes (headroom ${headroom}`;
  if (r.baselineSize === null) {
    return `${head}; delta (no baseline) — run --update-baseline)`;
  }
  const delta = r.size - r.baselineSize;
  if (delta > 0) {
    return `${head}; delta +${delta} vs baseline)`;
  }
  if (delta < 0) {
    return `${head}; delta ${delta} vs baseline — consider --update-baseline)`;
  }
  return `${head}; delta 0)`;
}

function failuresFor(r: CheckResult): string[] {
  if (r.size === null) {
    return r.required
      ? [
          `${r.name} artifact is missing but is required — run \`make solidity-build\` ` +
            `or check that the contract has not been renamed/removed.`,
        ]
      : [];
  }
  const errs: string[] = [];
  if (r.size > r.cap) {
    errs.push(
      `${r.name} exceeds cap by ${r.size - r.cap} bytes (${r.size}/${r.cap})`,
    );
  }
  if (
    r.headroomFloor !== null &&
    r.baselineSize !== null &&
    r.size - r.baselineSize > 0 &&
    r.cap - r.size < r.headroomFloor
  ) {
    errs.push(
      `${r.name} grew past the ${r.headroomFloor} B headroom floor ` +
        `(size ${r.size}, baseline ${r.baselineSize}, headroom ${r.cap - r.size}). ` +
        `If the growth is intentional, justify it and run \`bun run check:size:update\` to ratchet the baseline.`,
    );
  }
  return errs;
}

function diffLine(name: string, before: number | null, after: number): string {
  if (before === null) return `${name}: ${after} B (new)`;
  const delta = after - before;
  if (delta === 0) return `${name}: ${after} B (unchanged)`;
  if (delta > 0) return `${name}: ${after} B (+${delta} B)`;
  return `${name}: ${after} B (${delta} B)`;
}

async function updateBaseline(results: CheckResult[]): Promise<void> {
  const existing = (await loadBaseline()) ?? {
    version: 1,
    contracts: {},
    headroomFloor: {},
  };
  const next: BaselineFile = {
    version: existing.version,
    contracts: { ...existing.contracts },
    headroomFloor: existing.headroomFloor,
  };
  console.log("Updating baseline:");
  for (const r of results) {
    if (r.size === null) {
      console.log(`  ${r.name}: (skipped: artifact missing)`);
      continue;
    }
    console.log(`  ${diffLine(r.name, r.baselineSize, r.size)}`);
    next.contracts[r.name] = { size: r.size };
  }
  await writeBaseline(next);
  console.log(
    `Baseline written to ${path.relative(process.cwd(), BASELINE_PATH)}.`,
  );
}

async function main() {
  // Triggered via env var because `hardhat run` does not reliably forward
  // CLI args. Use `bun run check:size:update` (set in package.json) or
  // `UPDATE_BASELINE=1 hardhat run scripts/check-bytecode-size.ts --no-compile`.
  const updateMode =
    process.env.UPDATE_BASELINE === "1" ||
    process.argv.includes("--update-baseline");
  const baseline = await loadBaseline();
  if (!baseline && !updateMode) {
    console.warn(
      `WARN: ${path.relative(process.cwd(), BASELINE_PATH)} not found — ` +
        "delta and headroom-floor checks will be skipped. " +
        "Run with --update-baseline once to seed it.",
    );
  }

  const results = await Promise.all(
    CONTRACTS.map((c) =>
      check(c.name, c.limit - SIZE_BUFFER_BYTES, baseline, c.required),
    ),
  );

  if (updateMode) {
    await updateBaseline(results);
    return;
  }

  for (const r of results) {
    console.log(format(r));
    if (baseline && r.size !== null && r.baselineSize === null) {
      console.log(
        `  WARN: no baseline entry for ${r.name} — run \`bun run check:size:update\` to record one.`,
      );
    }
  }

  const failures = results.flatMap((r) => failuresFor(r));
  if (failures.length > 0) {
    const lines = failures.map((m) => `  ${m}`);
    throw new Error(`Bytecode size check failed:\n${lines.join("\n")}`);
  }

  console.log("Bytecode size checks passed.");
}

main().catch((error) => {
  console.error("Bytecode size check failed:", error.message);
  process.exit(1);
});
