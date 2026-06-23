// CreateX salt helpers + minimal ABI for the Rose bridge deployments.
//
// The salt helpers below replicate the two permissioned branches of CreateX's
// `_guard` function so we can predict CREATE3 addresses off-chain. Vendored
// 1:1 from pcaversaccio/createx tag v1.0.0, file `src/CreateX.sol`,
// function `_guard`. Only the branches this deploy uses are reproduced — every
// other upstream layout throws here.
//
// `deployCreate3(rawSalt, initCode)` consumes the *raw* salt; CreateX applies
// `_guard` internally. `computeCreate3Address(salt)` consumes a salt verbatim,
// so the *guarded* salt must be passed for off-chain address prediction.
// Mixing them silently predicts the wrong address.

import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

import { ethers } from "ethers";
import { task } from "hardhat/config";

export const CREATEX_ADDRESS = "0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed";

// xERC20 upstream pin — see contracts/bridge/README.md Pin table. Bumping this
// value is a re-vendor event; `.upstream-lock` separately tracks the sha256 of
// the vendored Solidity bytes. The DeployBridgeManifest tests assert this
// matches the README commit hash so docs-vs-constant drift fails CI.
export const XERC20_SOURCE =
  "defi-wonderland/xERC20@da2afabdeb1bad9ccda2f6eb928cd99e852530be";

export const CREATEX_ABI = [
  "function computeCreate3Address(bytes32 salt) view returns (address)",
  "function deployCreate3(bytes32 salt, bytes initCode) payable returns (address)",
];

// Default mint/burn cap for ROFLBridge on xROSE (testnet); override via --mint-limit / --burn-limit.
export const XROSE_DEFAULT_DAILY_LIMIT_HUMAN = "250000";

/**
 * Build a permissioned, same-address-multichain CreateX raw salt:
 *   bytes[0..19]  = deployer EOA
 *   bytes[20]     = 0x00  (redeploy-protection flag OFF → same address every chain)
 *   bytes[21..31] = first 11 bytes of keccak256(utf8(label))
 */
export function createXPermissionedSameAddressSalt(deployer: string, label: string): string {
  const deployerBytes = ethers.getBytes(ethers.getAddress(deployer));
  const labelHash = ethers.getBytes(ethers.keccak256(ethers.toUtf8Bytes(label)));
  const out = new Uint8Array(32);
  out.set(deployerBytes, 0);
  out[20] = 0x00;
  out.set(labelHash.slice(0, 11), 21);
  return ethers.hexlify(out);
}

/**
 * Mirror of CreateX `_guard` for the two permissioned branches this deploy uses.
 *
 * Branch A — byte21 == 0x00 (same-address multichain):
 *   keccak256(abi.encode(bytes32(uint256(uint160(deployer))), rawSalt))
 *
 * Branch B — byte21 == 0x01 (chain-id-locked, tests only):
 *   keccak256(abi.encode(bytes32(uint256(uint160(deployer))), bytes32(chainId), rawSalt))
 *
 * Any other layout (first20 != deployer, byte21 > 0x01, non-bytes32 input) throws.
 */
export function createXGuardedSalt(rawSalt: string, deployer: string, chainId: bigint): string {
  const saltBytes = ethers.getBytes(rawSalt);
  if (saltBytes.length !== 32) {
    throw new Error(
      `createXGuardedSalt: rawSalt must be bytes32 (got ${saltBytes.length} bytes)`,
    );
  }
  const deployerAddr = ethers.getAddress(deployer);
  const first20 = ethers.getAddress(ethers.hexlify(saltBytes.slice(0, 20)));
  const byte21 = saltBytes[20];

  if (first20 !== deployerAddr) {
    throw new Error(
      `createXGuardedSalt: unsupported salt layout. ` +
        `first20=${first20} must equal deployer=${deployerAddr}. ` +
        `Only permissioned salts (sender-bytes == deployer) are supported.`,
    );
  }

  const deployerWord = ethers.zeroPadValue(deployerAddr, 32);
  const coder = ethers.AbiCoder.defaultAbiCoder();

  if (byte21 === 0x00) {
    return ethers.keccak256(coder.encode(["bytes32", "bytes32"], [deployerWord, rawSalt]));
  }
  if (byte21 === 0x01) {
    const chainWord = ethers.toBeHex(chainId, 32);
    return ethers.keccak256(
      coder.encode(["bytes32", "bytes32", "bytes32"], [deployerWord, chainWord, rawSalt]),
    );
  }
  throw new Error(
    `createXGuardedSalt: unsupported byte21=0x${byte21.toString(16).padStart(2, "0")}. ` +
      `Only 0x00 (same-address multichain) and 0x01 (chain-id-locked, tests only) are supported.`,
  );
}

// ─── deployment manifest ────────────────────────────────────────────────────
//
// Schema mirrors solidity/deployments/bridge-<network>.json. The task writes
// this file post-deploy; preflight only *reads* it (if present) and compares
// predicted values. A missing manifest means first deploy of this network.

export interface BridgeManifest {
  network: string;
  chainId: number;
  createX: string;
  saltMode: "permissioned-no-cross-chain-redeploy-protection";
  xroseRawSalt: string;
  xroseGuardedSalt: string;
  xrose: string;
  roflBridgeRawSalt: string;
  roflBridgeGuardedSalt: string;
  roflBridgeSameAddressRequired: false;
  roflBridge: string;
  roflSigner: string;
  owner: string;
  factory: string;
  xerc20Source: string;
  xroseConstructor: [string, string, string];
  xroseRuntimeHash: string;
  mintLimit: string;
  burnLimit: string;
}

export function manifestPath(network: string): string {
  return join(__dirname, "..", "deployments", `bridge-${network}.json`);
}

export function readManifestIfExists(network: string): BridgeManifest | null {
  const path = manifestPath(network);
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, "utf8")) as BridgeManifest;
}

// ─── preflight: pure assertions ─────────────────────────────────────────────

export function assertCreateXDeployed(code: string, address: string): void {
  if (code === "0x" || code === "") {
    throw new Error(
      `preflight: CreateX not deployed at ${address}. ` +
        `Are you on the right network? This deploy requires CreateX as pre-existing infra.`,
    );
  }
}

// Proves raw != guarded so a no-op _guard can't slip through. Address
// correctness is further enforced post-broadcast by re-reading
// getCode(predicted) and the final parity check.
export function assertHelperRequired(rawPrediction: string, guardedPrediction: string): void {
  if (rawPrediction === guardedPrediction) {
    throw new Error(
      "preflight: computeCreate3Address(rawSalt) == computeCreate3Address(guardedSalt). " +
        "The _guard helper is a no-op for this salt — refusing to deploy with the wrong salt layout.",
    );
  }
}

export function assertManifestAgreement(
  predicted: Partial<BridgeManifest>,
  manifest: BridgeManifest | null,
): void {
  if (manifest === null) return;
  for (const key of Object.keys(predicted) as Array<keyof BridgeManifest>) {
    const expected = manifest[key];
    const actual = predicted[key];
    if (!deepEqual(expected, actual)) {
      throw new Error(
        `preflight: manifest mismatch — field=${String(key)}; ` +
          `expected=${stringify(expected)}; actual=${stringify(actual)}. ` +
          `The on-disk manifest disagrees with the values this deploy would produce. ` +
          `Investigate before broadcasting — a silent address change is the failure mode this catches.`,
      );
    }
  }
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  return a === b;
}

function stringify(v: unknown): string {
  return Array.isArray(v) ? JSON.stringify(v) : String(v);
}

// ─── manifest writer ────────────────────────────────────────────────────────
//
// `buildBridgeManifest` is a pure constructor; it stitches the in-scope deploy
// outputs into the canonical `BridgeManifest` shape. `reconcileManifest`
// compares a proposed write against any existing on-disk manifest and decides
// whether to write, no-op, or refuse. `writeManifestAtPath` performs the
// atomic write (write to `.tmp`, then rename). `writeBridgeManifestAtomic` is
// the thin path-composing wrapper used by the deploy task.

export interface BuildBridgeManifestArgs {
  network: string;
  chainId: number | bigint;
  createX: string;
  xroseAddress: string;
  xroseRawSalt: string;
  xroseGuardedSalt: string;
  xroseRuntimeHash: string;
  xroseConstructor: [string, string, string];
  roflBridgeAddress: string;
  roflBridgeRawSalt: string;
  roflBridgeGuardedSalt: string;
  roflSigner: string;
  owner: string;
  factory: string;
  mintLimit: bigint;
  burnLimit: bigint;
}

export function buildBridgeManifest(a: BuildBridgeManifestArgs): BridgeManifest {
  return {
    network: a.network,
    chainId: Number(a.chainId),
    createX: a.createX,
    saltMode: "permissioned-no-cross-chain-redeploy-protection",
    xroseRawSalt: a.xroseRawSalt,
    xroseGuardedSalt: a.xroseGuardedSalt,
    xrose: a.xroseAddress,
    roflBridgeRawSalt: a.roflBridgeRawSalt,
    roflBridgeGuardedSalt: a.roflBridgeGuardedSalt,
    roflBridgeSameAddressRequired: false,
    roflBridge: a.roflBridgeAddress,
    roflSigner: a.roflSigner,
    owner: a.owner,
    factory: a.factory,
    xerc20Source: XERC20_SOURCE,
    xroseConstructor: a.xroseConstructor,
    xroseRuntimeHash: a.xroseRuntimeHash,
    mintLimit: a.mintLimit.toString(),
    burnLimit: a.burnLimit.toString(),
  };
}

export type ReconcileAction = "write" | "update" | "noop";

export interface ReconcileResult {
  action: ReconcileAction;
  manifest: BridgeManifest;
  changedFields: string[];
}

// Fields safe to update without operator confirmation. Limits are legitimately
// re-broadcastable via setLimits; every other field is an identity invariant
// (addresses, salts, runtime hash, provenance).
const UPDATABLE_FIELDS: ReadonlyArray<keyof BridgeManifest> = ["mintLimit", "burnLimit"];

export function reconcileManifest(
  existing: BridgeManifest | null,
  proposed: BridgeManifest,
): ReconcileResult {
  if (existing === null) {
    return { action: "write", manifest: proposed, changedFields: [] };
  }
  const changedFields: Array<keyof BridgeManifest> = [];
  for (const key of Object.keys(proposed) as Array<keyof BridgeManifest>) {
    if (!deepEqual(existing[key], proposed[key])) changedFields.push(key);
  }
  if (changedFields.length === 0) {
    return { action: "noop", manifest: existing, changedFields: [] };
  }
  const offending = changedFields.filter((k) => !UPDATABLE_FIELDS.includes(k));
  if (offending.length > 0) {
    const lines = offending.map(
      (k) =>
        `  ${String(k)}: existing=${stringify(existing[k])} proposed=${stringify(proposed[k])}`,
    );
    throw new Error(
      `manifest mismatch — refusing to overwrite immutable fields:\n${lines.join("\n")}\n` +
        `Investigate before re-running — a silent address change is the failure mode this catches.`,
    );
  }
  return { action: "update", manifest: proposed, changedFields: changedFields.map(String) };
}

export async function writeManifestAtPath(
  absolutePath: string,
  manifest: BridgeManifest,
): Promise<string> {
  const tmp = `${absolutePath}.tmp`;
  await mkdir(dirname(absolutePath), { recursive: true });
  const body = JSON.stringify(manifest, null, 2) + "\n";
  await writeFile(tmp, body, { encoding: "utf8" });
  await rename(tmp, absolutePath);
  return absolutePath;
}

export async function writeBridgeManifestAtomic(
  network: string,
  manifest: BridgeManifest,
): Promise<string> {
  return writeManifestAtPath(manifestPath(network), manifest);
}

// ─── upstream-lock ──────────────────────────────────────────────────────────
//
// `.upstream-lock` lines are tab-separated triples:
//   <upstream-path>\t<vendor-path>\t<sha256-hex-64-chars>\n
// Re-hashing the local vendored bytes and comparing to the locked hash catches
// "someone edited the vendored Solidity after pinning". A separate verification
// script covers the network roundtrip; this only does the local check here.

export interface UpstreamLockEntry {
  upstreamPath: string;
  vendorPath: string;
  sha256: string;
}

export function parseUpstreamLock(text: string): UpstreamLockEntry[] {
  const out: UpstreamLockEntry[] = [];
  const lines = text.split("\n");
  for (const line of lines) {
    if (line.trim() === "") continue;
    const parts = line.split("\t");
    if (parts.length !== 3) {
      throw new Error(`malformed .upstream-lock line (want 3 tab-separated columns): ${line}`);
    }
    const [upstreamPath, vendorPath, sha256] = parts;
    if (!/^[0-9a-f]{64}$/.test(sha256)) {
      throw new Error(`malformed .upstream-lock line — sha256 must be 64 lowercase hex chars: ${sha256}`);
    }
    out.push({ upstreamPath, vendorPath, sha256 });
  }
  return out;
}

export function readUpstreamLock(repoRoot: string): UpstreamLockEntry[] {
  const path = join(repoRoot, "contracts", "bridge", ".upstream-lock");
  return parseUpstreamLock(readFileSync(path, "utf8"));
}

export function assertUpstreamLockIntact(
  lockEntries: Array<{ vendorPath: string; sha256: string }>,
  readLocalBytes: (vendorPath: string) => Buffer,
): void {
  for (const { vendorPath, sha256 } of lockEntries) {
    const actual = createHash("sha256").update(readLocalBytes(vendorPath)).digest("hex");
    if (actual !== sha256) {
      throw new Error(
        `preflight: upstream-lock drift detected for ${vendorPath}. ` +
          `expected sha256=${sha256}, got sha256=${actual}. ` +
          `The vendored source was edited after pinning — re-vendor or update .upstream-lock.`,
      );
    }
  }
}

// ─── preflight predictions ──────────────────────────────────────────────────
//
// `buildXrosePreflight` produces every value a CREATE3 deploy of (XRose,
// ROFLBridge) needs *before* talking to CreateX. ROFLBridge gets raw+guarded
// salt only — its initcode requires the *predicted* xROSE address, which the
// task body builds after `computeCreate3Address`.
//
// Salt labels are derived from a single base label so a future chain reusing
// `XRose:phase0` lands at the same xROSE/ROFLBridge addresses:
//   xROSE   → `${label}:xrose`
//   bridge  → `${label}:bridge`

export interface PreflightInputs {
  chainId: bigint;
  deployer: string;
  xroseName: string;
  xroseSymbol: string;
  label: string;
  xroseArtifact: { bytecode: string; deployedBytecode: string };
}

export interface PreflightPredictions {
  xroseRawSalt: string;
  xroseGuardedSalt: string;
  xroseInitCode: string;
  xroseRuntimeHash: string;
  roflBridgeRawSalt: string;
  roflBridgeGuardedSalt: string;
}

export function buildXrosePreflight(i: PreflightInputs): PreflightPredictions {
  const xroseRawSalt = createXPermissionedSameAddressSalt(i.deployer, `${i.label}:xrose`);
  const xroseGuardedSalt = createXGuardedSalt(xroseRawSalt, i.deployer, i.chainId);

  const xroseArgs = ethers.AbiCoder.defaultAbiCoder().encode(
    ["string", "string", "address"],
    [i.xroseName, i.xroseSymbol, ethers.getAddress(i.deployer)],
  );
  const xroseInitCode = i.xroseArtifact.bytecode + xroseArgs.slice(2);
  const xroseRuntimeHash = ethers.keccak256(i.xroseArtifact.deployedBytecode);

  const roflBridgeRawSalt = createXPermissionedSameAddressSalt(i.deployer, `${i.label}:bridge`);
  const roflBridgeGuardedSalt = createXGuardedSalt(roflBridgeRawSalt, i.deployer, i.chainId);

  return {
    xroseRawSalt,
    xroseGuardedSalt,
    xroseInitCode,
    xroseRuntimeHash,
    roflBridgeRawSalt,
    roflBridgeGuardedSalt,
  };
}

export function buildRoflBridgeInitCode(
  artifact: { bytecode: string },
  predictedXrose: string,
  roflSigner: string,
  pauseAdmin: string,
  owner: string,
): string {
  const args = ethers.AbiCoder.defaultAbiCoder().encode(
    ["address", "address", "address", "address"],
    [
      ethers.getAddress(predictedXrose),
      ethers.getAddress(roflSigner),
      ethers.getAddress(pauseAdmin),
      ethers.getAddress(owner),
    ],
  );
  return artifact.bytecode + args.slice(2);
}

// ─── broadcast: CreateX deployCreate3 ───────────────────────────────────────
//
// CreateX consumes the *raw* permissioned salt and applies `_guard` internally
// to derive the deployment address. The *guarded* salt is what
// `computeCreate3Address` expects off-chain. Mixing the two silently predicts
// the wrong address — the in-task `assertHelperRequired` guards against that.
//
// `deployCreate3IfMissing` is idempotent: a re-run that finds code at the
// predicted address skips the broadcast and returns `reused: true`. The
// `name` argument is only used to label error messages.

export interface DeployCreate3Result {
  address: string;
  txHash: string | null;
  reused: boolean;
}

export async function deployCreate3IfMissing(
  name: string,
  createx: ethers.Contract,
  provider: ethers.Provider,
  predictedAddress: string,
  rawSalt: string,
  initCode: string,
): Promise<DeployCreate3Result> {
  const preCode = await provider.getCode(predictedAddress);
  if (preCode !== "0x" && preCode !== "") {
    return { address: predictedAddress, txHash: null, reused: true };
  }

  const tx = await createx.deployCreate3(rawSalt, initCode);
  const receipt = await tx.wait();
  if (!receipt || receipt.status !== 1) {
    throw new Error(
      `${name} deploy tx ${tx.hash} did not succeed (status=${receipt?.status ?? "null"}).`,
    );
  }

  const postCode = await provider.getCode(predictedAddress);
  if (postCode === "0x" || postCode === "") {
    throw new Error(
      `${name} deploy tx ${tx.hash} confirmed but predicted address ${predictedAddress} has no code. ` +
        `CreateX may have deployed elsewhere — inspect the transaction receipt.`,
    );
  }

  return { address: predictedAddress, txHash: tx.hash, reused: false };
}

// ─── post-deploy assertion: ROFLBridge wiring ───────────────────────────────
//
// CREATE3 derives the deploy address from `(salt, deployer)` only — initcode
// and constructor arguments do not affect the address. So a bridge deployed
// with the *wrong* xROSE/roflSigner/pauseAdmin immutables lands at the same
// predicted address as one deployed with the right ones. Only a post-deploy
// read of the immutables can catch this. This helper runs on both the
// fresh-deploy and idempotent-reuse paths so a stale bridge from a botched
// prior run is rejected loudly instead of silently accepted.

export interface BridgeWiringExpectations {
  xrose: string;
  roflSigner: string;
  pauseAdmin: string;
  owner: string;
}

export async function assertRoflBridgeWiring(
  bridge: ethers.Contract,
  expected: BridgeWiringExpectations,
): Promise<void> {
  const actual: BridgeWiringExpectations = {
    xrose: ethers.getAddress(await bridge.xrose()),
    roflSigner: ethers.getAddress(await bridge.roflSigner()),
    pauseAdmin: ethers.getAddress(await bridge.pauseAdmin()),
    owner: ethers.getAddress(await bridge.owner()),
  };
  const want: BridgeWiringExpectations = {
    xrose: ethers.getAddress(expected.xrose),
    roflSigner: ethers.getAddress(expected.roflSigner),
    pauseAdmin: ethers.getAddress(expected.pauseAdmin),
    owner: ethers.getAddress(expected.owner),
  };
  for (const field of ["xrose", "roflSigner", "pauseAdmin", "owner"] as const) {
    if (actual[field] !== want[field]) {
      throw new Error(
        `ROFLBridge wiring: ${field} mismatch — expected ${want[field]}, got ${actual[field]}.`,
      );
    }
  }
}

// Idempotent `xrose.setLimits(bridge, mintLimit, burnLimit)` wrapper. Skips the
// broadcast when both stored caps already match — `mintingMaxLimitOf` returns
// the per-bridge `maxLimit` (not the replenishing counter), so equality is a
// stable check even under partial mint activity. Bubbles XRose's own custom
// errors (e.g. `OwnableUnauthorizedAccount`) for callers to catch.
export async function configureXroseLimitsIfNeeded(
  xrose: ethers.Contract,
  bridge: string,
  mintLimit: bigint,
  burnLimit: bigint,
): Promise<{ skipped: boolean; txHash: string | null }> {
  const currentMint: bigint = await xrose.mintingMaxLimitOf(bridge);
  const currentBurn: bigint = await xrose.burningMaxLimitOf(bridge);
  if (currentMint === mintLimit && currentBurn === burnLimit) {
    return { skipped: true, txHash: null };
  }
  const tx = await xrose.setLimits(bridge, mintLimit, burnLimit);
  const receipt = await tx.wait();
  if (!receipt || receipt.status !== 1) {
    throw new Error(`xROSE setLimits tx ${tx.hash} did not succeed.`);
  }
  return { skipped: false, txHash: tx.hash };
}

export interface XroseBridgeConfig {
  mintLimit: bigint;
  burnLimit: bigint;
  owner: string;
  factory: string;
  roflSigner: string;
}

// Read-back guard for the five config values: bridge mint/burn
// caps on xROSE, xROSE owner + FACTORY, and ROFLBridge's roflSigner. Aggregates
// every mismatch into one error so operators see the full drift in a single
// log line — misaligned-config scenarios typically drift in multiple places at
// once.
export async function assertXroseBridgeConfig(
  xrose: ethers.Contract,
  bridge: ethers.Contract,
  bridgeAddr: string,
  expected: XroseBridgeConfig,
): Promise<void> {
  const actualMintLimit: bigint = await xrose.mintingMaxLimitOf(bridgeAddr);
  const actualBurnLimit: bigint = await xrose.burningMaxLimitOf(bridgeAddr);
  const actualOwner = ethers.getAddress(await xrose.owner());
  const actualFactory = ethers.getAddress(await xrose.FACTORY());
  const actualLockbox = ethers.getAddress(await xrose.lockbox());
  const actualRoflSigner = ethers.getAddress(await bridge.roflSigner());
  const wantOwner = ethers.getAddress(expected.owner);
  const wantFactory = ethers.getAddress(expected.factory);
  const wantRoflSigner = ethers.getAddress(expected.roflSigner);

  const mismatches: string[] = [];
  if (actualMintLimit !== expected.mintLimit) {
    mismatches.push(
      `mintLimit mismatch — expected ${expected.mintLimit}, got ${actualMintLimit}`,
    );
  }
  if (actualBurnLimit !== expected.burnLimit) {
    mismatches.push(
      `burnLimit mismatch — expected ${expected.burnLimit}, got ${actualBurnLimit}`,
    );
  }
  if (actualOwner !== wantOwner) {
    mismatches.push(`owner mismatch — expected ${wantOwner}, got ${actualOwner}`);
  }
  if (actualFactory !== wantFactory) {
    mismatches.push(`FACTORY mismatch — expected ${wantFactory}, got ${actualFactory}`);
  }
  // A non-zero lockbox in XRose._burnWithCaller/_mintWithCaller skips the
  // per-bridge limit check entirely; we treat any set lockbox as a
  // misconfiguration so the bridge can't silently bypass mint/burn caps.
  if (actualLockbox !== ethers.ZeroAddress) {
    mismatches.push(
      `lockbox mismatch — expected ${ethers.ZeroAddress}, got ${actualLockbox}`,
    );
  }
  if (actualRoflSigner !== wantRoflSigner) {
    mismatches.push(
      `roflSigner mismatch — expected ${wantRoflSigner}, got ${actualRoflSigner}`,
    );
  }
  if (mismatches.length > 0) {
    throw new Error(`xROSE/bridge config: ${mismatches.join("; ")}.`);
  }
}

// ─── task ───────────────────────────────────────────────────────────────────
//
// `npx hardhat deploy-bridge --dry-run --network base-sepolia` runs every
// preflight check and prints the predicted xROSE/ROFLBridge state. Without
// `--dry-run` the task additionally broadcasts xROSE + ROFLBridge via
// CREATE3, verifies the bridge wiring on-chain, sets the bridge mint/burn
// caps on xROSE (idempotent on re-run), asserts the mutual xROSE/bridge
// config, and writes the deployment manifest to
// `solidity/deployments/bridge-<network>.json` (atomic write; idempotent on
// re-run; refuses to overwrite immutable fields). Operators follow up on
// sapphire-testnet with `bridge:addRoseToken`; the Accounting.roflBridgeAddress
// route entry is written by the in-TEE bridge route reconciler once the
// container starts with ROFL_BRIDGE_ADDRESS set to the bridge address
// printed below.

task("deploy-bridge", "Preflight + CREATE3 deploy of XRose and ROFLBridge")
  .addFlag("dryRun", "Run all preflight checks, print predicted state, exit without broadcasting.")
  .addOptionalParam(
    "accounting",
    "Accounting proxy on sapphire-testnet (default: $ACCOUNTING_ADDRESS_SAPPHIRE)",
  )
  .addOptionalParam("label", "Base salt label", "XRose:phase0")
  .addOptionalParam("xroseName", "XRose ERC20 name", "XRose")
  .addOptionalParam("xroseSymbol", "XRose ERC20 symbol", "xROSE")
  .addOptionalParam("pauseAdmin", "ROFLBridge pause admin (default: deployer signer)")
  .addOptionalParam(
    "mintLimit",
    "ROFLBridge mint cap (human-readable, parsed via parseEther)",
    XROSE_DEFAULT_DAILY_LIMIT_HUMAN,
  )
  .addOptionalParam(
    "burnLimit",
    "ROFLBridge burn cap (human-readable, parsed via parseEther)",
    XROSE_DEFAULT_DAILY_LIMIT_HUMAN,
  )
  .setAction(async (args, hre) => {
    // 1. Resolve deployer + chainId on the target network.
    const [signer] = await hre.ethers.getSigners();
    const deployer = hre.ethers.getAddress(signer.address);
    const { chainId } = await hre.ethers.provider.getNetwork();

    // 2. Resolve accounting address — CLI overrides env.
    const accountingAddress =
      (args.accounting as string | undefined) ?? process.env.ACCOUNTING_ADDRESS_SAPPHIRE;
    if (!accountingAddress) {
      throw new Error(
        "preflight: no Accounting proxy address. " +
          "Pass --accounting <addr> or set $ACCOUNTING_ADDRESS_SAPPHIRE.",
      );
    }
    if (!hre.ethers.isAddress(accountingAddress)) {
      throw new Error(`preflight: invalid Accounting address: ${accountingAddress}`);
    }

    // 3. Read roflSigner from sapphire-testnet Accounting via a fresh provider.
    const sapphireCfg = hre.config.networks["sapphire-testnet"];
    if (!sapphireCfg || !("url" in sapphireCfg) || !sapphireCfg.url) {
      throw new Error(
        "preflight: sapphire-testnet network has no RPC URL configured in hardhat.config.ts",
      );
    }
    const sapphireProvider = new ethers.JsonRpcProvider(sapphireCfg.url);
    const accounting = new ethers.Contract(
      accountingAddress,
      ["function evmAddress() view returns (address)"],
      sapphireProvider,
    );
    const roflSigner: string = hre.ethers.getAddress(await accounting.evmAddress());

    const pauseAdmin = hre.ethers.getAddress((args.pauseAdmin as string | undefined) ?? deployer);

    // 4. CreateX presence on the deploy network.
    const createxCode = await hre.ethers.provider.getCode(CREATEX_ADDRESS);
    assertCreateXDeployed(createxCode, CREATEX_ADDRESS);

    // 5. Upstream-lock provenance: re-hash local vendored files.
    const solidityRoot = join(__dirname, "..");
    const lock = readUpstreamLock(solidityRoot);
    assertUpstreamLockIntact(lock, (vendorPath) => {
      // `.upstream-lock` records repo-rooted paths (e.g. `solidity/contracts/bridge/XRose.sol`).
      // Strip the leading `solidity/` segment so the read is relative to `solidityRoot`.
      const rel = vendorPath.startsWith("solidity/") ? vendorPath.slice("solidity/".length) : vendorPath;
      return readFileSync(join(solidityRoot, rel));
    });

    // 6. Load XRose + ROFLBridge artifacts and build predictions.
    const xroseArtifact = await hre.artifacts.readArtifact("XRose");
    const bridgeArtifact = await hre.artifacts.readArtifact("ROFLBridge");
    const predictions = buildXrosePreflight({
      chainId,
      deployer,
      xroseName: args.xroseName as string,
      xroseSymbol: args.xroseSymbol as string,
      label: args.label as string,
      xroseArtifact: {
        bytecode: xroseArtifact.bytecode,
        deployedBytecode: xroseArtifact.deployedBytecode,
      },
    });

    // 7. xROSE: ask CreateX for both raw-salt and guarded-salt predictions; assert helper required.
    const createx = new hre.ethers.Contract(CREATEX_ADDRESS, CREATEX_ABI, hre.ethers.provider);
    const predictedXroseFromGuarded: string = await createx.computeCreate3Address(predictions.xroseGuardedSalt);
    const predictedXroseFromRaw: string = await createx.computeCreate3Address(predictions.xroseRawSalt);
    assertHelperRequired(predictedXroseFromRaw, predictedXroseFromGuarded);
    const predictedXrose = hre.ethers.getAddress(predictedXroseFromGuarded);

    // 8. ROFLBridge: build initcode using the predicted xROSE address, repeat the prediction + parity check.
    const roflBridgeInitCode = buildRoflBridgeInitCode(
      { bytecode: bridgeArtifact.bytecode },
      predictedXrose,
      roflSigner,
      pauseAdmin,
      deployer,
    );
    const predictedBridgeFromGuarded: string = await createx.computeCreate3Address(predictions.roflBridgeGuardedSalt);
    const predictedBridgeFromRaw: string = await createx.computeCreate3Address(predictions.roflBridgeRawSalt);
    assertHelperRequired(predictedBridgeFromRaw, predictedBridgeFromGuarded);
    const predictedBridge = hre.ethers.getAddress(predictedBridgeFromGuarded);

    // 9. Manifest agreement (if a manifest exists for this network).
    const manifest = readManifestIfExists(hre.network.name);
    const predicted: Partial<BridgeManifest> = {
      xrose: predictedXrose,
      xroseRawSalt: predictions.xroseRawSalt,
      xroseGuardedSalt: predictions.xroseGuardedSalt,
      roflBridge: predictedBridge,
      roflBridgeRawSalt: predictions.roflBridgeRawSalt,
      roflBridgeGuardedSalt: predictions.roflBridgeGuardedSalt,
      roflSigner,
      owner: deployer,
      factory: deployer,
      xroseConstructor: [args.xroseName as string, args.xroseSymbol as string, deployer],
      // `xroseRuntimeHash` omitted: the manifest stores the on-chain hash,
      // not the artifact hash, so there's nothing to compare at preflight.
    };
    assertManifestAgreement(predicted, manifest);

    // 10. Pretty-print predicted state.
    console.log("preflight OK");
    console.log(`network              : ${hre.network.name} (chainId=${chainId})`);
    console.log(`deployer             : ${deployer}`);
    console.log(`accounting (sapphire): ${accountingAddress}`);
    console.log(`roflSigner           : ${roflSigner}`);
    console.log(`pauseAdmin           : ${pauseAdmin}`);
    console.log(`label                : ${args.label}`);
    console.log(`xroseConstructor     : ${JSON.stringify(predicted.xroseConstructor)}`);
    console.log(`xroseRawSalt         : ${predictions.xroseRawSalt}`);
    console.log(`xroseGuardedSalt     : ${predictions.xroseGuardedSalt}`);
    console.log(`xroseInitCode (sha)  : ${ethers.keccak256(predictions.xroseInitCode)}`);
    console.log(`xroseRuntimeHash     : ${predictions.xroseRuntimeHash}`);
    console.log(`xrose (predicted)    : ${predictedXrose}`);
    console.log(`roflBridgeRawSalt    : ${predictions.roflBridgeRawSalt}`);
    console.log(`roflBridgeGuardedSalt: ${predictions.roflBridgeGuardedSalt}`);
    console.log(`roflBridgeInitCode(sha): ${ethers.keccak256(roflBridgeInitCode)}`);
    console.log(`roflBridge (predicted): ${predictedBridge}`);
    console.log(
      manifest
        ? "manifest agrees with predicted state."
        : `no manifest at ${manifestPath(hre.network.name)} — values above will be captured post-deploy.`,
    );

    if (args.dryRun) {
      console.log("dry-run: preflight complete, no broadcast.");
      return;
    }

    // 11. Live broadcast — xROSE.
    const createxSigner = new hre.ethers.Contract(CREATEX_ADDRESS, CREATEX_ABI, signer);
    const xroseResult = await deployCreate3IfMissing(
      "xROSE",
      createxSigner,
      hre.ethers.provider,
      predictedXrose,
      predictions.xroseRawSalt,
      predictions.xroseInitCode,
    );
    console.log(
      xroseResult.reused
        ? `xROSE already at ${xroseResult.address} (idempotent skip).`
        : `xROSE deployed at ${xroseResult.address} via tx ${xroseResult.txHash}.`,
    );

    // 12. Live broadcast — ROFLBridge.
    const bridgeResult = await deployCreate3IfMissing(
      "ROFLBridge",
      createxSigner,
      hre.ethers.provider,
      predictedBridge,
      predictions.roflBridgeRawSalt,
      roflBridgeInitCode,
    );
    console.log(
      bridgeResult.reused
        ? `ROFLBridge already at ${bridgeResult.address} (idempotent skip).`
        : `ROFLBridge deployed at ${bridgeResult.address} via tx ${bridgeResult.txHash}.`,
    );

    // 13. Verify wiring — runs on both fresh-deploy and reused paths.
    const bridge = new hre.ethers.Contract(
      bridgeResult.address,
      [
        "function xrose() view returns (address)",
        "function roflSigner() view returns (address)",
        "function pauseAdmin() view returns (address)",
        "function owner() view returns (address)",
      ],
      hre.ethers.provider,
    );
    await assertRoflBridgeWiring(bridge, {
      xrose: predictedXrose,
      roflSigner,
      pauseAdmin,
      owner: deployer,
    });
    console.log(
      `ROFLBridge wiring OK — xrose=${predictedXrose} roflSigner=${roflSigner} ` +
        `pauseAdmin=${pauseAdmin} owner=${deployer}.`,
    );

    // 14. Configure xROSE rate limits for ROFLBridge (idempotent on re-run).
    const mintLimit = hre.ethers.parseEther(String(args.mintLimit));
    const burnLimit = hre.ethers.parseEther(String(args.burnLimit));
    const xroseSigned = new hre.ethers.Contract(
      xroseResult.address,
      [
        "function setLimits(address,uint256,uint256)",
        "function mintingMaxLimitOf(address) view returns (uint256)",
        "function burningMaxLimitOf(address) view returns (uint256)",
        "function owner() view returns (address)",
        "function FACTORY() view returns (address)",
        "function lockbox() view returns (address)",
      ],
      signer,
    );
    const limitsResult = await configureXroseLimitsIfNeeded(
      xroseSigned,
      bridgeResult.address,
      mintLimit,
      burnLimit,
    );
    console.log(
      limitsResult.skipped
        ? `xROSE limits already at mintLimit=${mintLimit} burnLimit=${burnLimit} (idempotent skip).`
        : `xROSE setLimits broadcast via tx ${limitsResult.txHash} (mintLimit=${mintLimit}, burnLimit=${burnLimit}).`,
    );

    // 15. Verify mutual xROSE/bridge config — runs on both skipped and broadcast paths.
    await assertXroseBridgeConfig(xroseSigned, bridge, bridgeResult.address, {
      mintLimit,
      burnLimit,
      owner: deployer,
      factory: deployer,
      roflSigner,
    });
    console.log(
      `xROSE/bridge config OK — mintLimit=${mintLimit} burnLimit=${burnLimit} ` +
        `owner=${deployer} FACTORY=${deployer} lockbox=${ethers.ZeroAddress} ` +
        `roflSigner=${roflSigner}.`,
    );

    // 16. Address parity + record on-chain xROSE runtime hash. Not compared
    //     to the artifact hash — XRose has an `immutable FACTORY` (XRose.sol:18)
    //     which is zero-padded in `deployedBytecode` but resolved on-chain.
    if (xroseResult.address !== predictedXrose) {
      throw new Error(
        `manifest: xROSE deployed address ${xroseResult.address} != predicted ${predictedXrose}.`,
      );
    }
    if (bridgeResult.address !== predictedBridge) {
      throw new Error(
        `manifest: ROFLBridge deployed address ${bridgeResult.address} != predicted ${predictedBridge}.`,
      );
    }
    const onchainXroseRuntime = await hre.ethers.provider.getCode(xroseResult.address);
    const onchainXroseRuntimeHash = ethers.keccak256(onchainXroseRuntime);

    // 17. Build proposed manifest and reconcile against any existing file.
    const proposed = buildBridgeManifest({
      network: hre.network.name,
      chainId,
      createX: CREATEX_ADDRESS,
      xroseAddress: xroseResult.address,
      xroseRawSalt: predictions.xroseRawSalt,
      xroseGuardedSalt: predictions.xroseGuardedSalt,
      xroseRuntimeHash: onchainXroseRuntimeHash,
      xroseConstructor: [args.xroseName as string, args.xroseSymbol as string, deployer],
      roflBridgeAddress: bridgeResult.address,
      roflBridgeRawSalt: predictions.roflBridgeRawSalt,
      roflBridgeGuardedSalt: predictions.roflBridgeGuardedSalt,
      roflSigner,
      owner: deployer,
      factory: deployer,
      mintLimit,
      burnLimit,
    });
    const existingManifest = readManifestIfExists(hre.network.name);
    const reconciled = reconcileManifest(existingManifest, proposed);
    if (reconciled.action === "noop") {
      console.log(
        `manifest already current at ${manifestPath(hre.network.name)} (idempotent).`,
      );
    } else {
      const written = await writeBridgeManifestAtomic(hre.network.name, reconciled.manifest);
      console.log(
        reconciled.action === "write"
          ? `manifest written to ${written}.`
          : `manifest updated at ${written} (changed: ${reconciled.changedFields.join(", ")}).`,
      );
    }

    console.log(
      `Next steps:\n` +
        `  1. bun hardhat bridge:addRoseToken --network sapphire-testnet\n` +
        `  2. oasis rofl secrets set ROFL_BRIDGE_ADDRESS ${bridgeResult.address}\n` +
        `  3. oasis rofl deploy\n` +
        `  The in-TEE reconciler writes Accounting.roflBridgeAddress[84532] on its next tick.`,
    );
  });
