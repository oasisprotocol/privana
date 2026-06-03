import { artifacts } from "hardhat";

/// Reads the storage layout JSON that solc emits into build-info, keyed by
/// (source file, contract name). The layout is part of the artifact-extras
/// file produced when `outputSelection` includes `storageLayout` — Hardhat
/// preserves it when writing build-info.
export async function readStorageLayout(name: string): Promise<any> {
  const fqName = (await artifacts.getAllFullyQualifiedNames()).find((n) =>
    n.endsWith(`:${name}`),
  );
  if (!fqName) {
    throw new Error(`Could not find fully qualified name for ${name}`);
  }
  const buildInfo = await artifacts.getBuildInfo(fqName);
  if (!buildInfo) {
    throw new Error(
      `No build-info for ${fqName} — did you run hardhat compile?`,
    );
  }
  const [src, cname] = fqName.split(":");
  const out = buildInfo.output.contracts?.[src]?.[cname] as any;
  const layout = out?.storageLayout;
  if (!layout) {
    throw new Error(
      `No storageLayout in build-info for ${fqName}. Add 'storageLayout' to ` +
        `compilerOptions.outputSelection in hardhat.config.ts if missing.`,
    );
  }
  return layout;
}

/// Returns the slot index (as bigint) for the named state variable in the
/// given contract's storage layout. Throws if the variable is not declared.
export async function findSlot(
  contractName: string,
  variable: string,
): Promise<bigint> {
  const layout = await readStorageLayout(contractName);
  const entry = (layout.storage as any[]).find((e) => e.label === variable);
  if (!entry) {
    throw new Error(
      `Variable ${variable} not found in ${contractName} storage layout`,
    );
  }
  return BigInt(entry.slot);
}
