import { expect } from "chai";

import { readStorageLayout } from "./util/storage";

// solc embeds per-compilation AST node IDs inside type strings for structs,
// contracts, and enums (e.g. `t_struct(TokenInfo)14925_storage`). When hardhat
// incremental-compiles into multiple build-info files, the same struct gets a
// different ID in each compilation unit and a strict `===` compare on the type
// string false-positives even though the underlying layout is identical. CI is
// safe (one build-info per fresh run); local incremental compiles are not.
// Strip the trailing IDs only for these constructs — array lengths
// (`t_array(t_uint256)38_storage`) MUST be preserved.
function normalizeType(t: string): string {
  return t.replace(/t_(struct|contract|enum)\(([^)]+)\)\d+/g, "t_$1($2)");
}

// For every variable `child` declares (inherited or own), assert it sits at
// the SAME slot/offset/type/label in `parent`'s layout. UUPSUpgradeable
// contributes zero structured slots, so the inheritance position match holds
// even though Accounting also inherits UUPS. Multiple `__gap` arrays exist
// (one per base contract that uses gaps), so we key by `{slot}:{offset}`
// rather than label.
function assertSharedPrefix(childLayout: any, parentLayout: any): void {
  const parentIndex = new Map<string, any>();
  for (const e of parentLayout.storage as any[]) {
    parentIndex.set(`${e.slot}:${e.offset}`, e);
  }

  for (const m of childLayout.storage as any[]) {
    const key = `${m.slot}:${m.offset}`;
    const a = parentIndex.get(key);
    if (a === undefined) {
      throw new Error(
        `parent has no var at slot ${key} (child declares ${m.label})`,
      );
    }
    if (m.label !== a.label) {
      throw new Error(
        `Label drift at slot ${key} — child: ${m.label}, parent: ${a.label}`,
      );
    }
    if (normalizeType(m.type) !== normalizeType(a.type)) {
      throw new Error(
        `Type drift at slot ${key} on label ${m.label} — ` +
          `child: ${m.type}, parent: ${a.type}`,
      );
    }
  }
}

describe("Accounting / BridgeModule storage layout", () => {
  it("BridgeModule storage layout matches Accounting (no slot drift on shared variables)", async () => {
    const acctLayout = await readStorageLayout("Accounting");
    const moduleLayout = await readStorageLayout("BridgeModule");
    assertSharedPrefix(moduleLayout, acctLayout);
  });

  it("BadBridgeModule layout fails the prefix gate", async () => {
    // Sanity check: the wrong-layout fixture (extra slot inserted after
    // AccountingStorage's `__gap` via an intermediate abstract base) MUST
    // make the prefix gate throw. Keeps the gate honest — without this
    // test, a vacuous matcher would silently green-light real layout drift.
    const acctLayout = await readStorageLayout("Accounting");
    const badLayout = await readStorageLayout("BadBridgeModule");
    expect(() => assertSharedPrefix(badLayout, acctLayout)).to.throw(
      /no var at slot|Label drift|Type drift/,
    );
  });

  it("__gap entries on Accounting are mirrored in BridgeModule (local gap-shrinkage gate)", async () => {
    // OZ's upgrade validator catches gap shrinkage on actual upgrades; this
    // is a faster local gate that runs every test cycle without spinning up
    // a proxy. Walk every `__gap` entry on Accounting and assert the same
    // {slot, offset, type} appears on BridgeModule's layout.
    const acctLayout = await readStorageLayout("Accounting");
    const moduleLayout = await readStorageLayout("BridgeModule");
    const moduleIndex = new Map<string, any>();
    for (const e of moduleLayout.storage as any[]) {
      moduleIndex.set(`${e.slot}:${e.offset}`, e);
    }
    const gaps = (acctLayout.storage as any[]).filter(
      (e) => e.label === "__gap",
    );
    expect(gaps.length).to.be.greaterThan(
      0,
      "Accounting has no __gap entries — refactor likely removed them",
    );
    for (const g of gaps) {
      const key = `${g.slot}:${g.offset}`;
      const m = moduleIndex.get(key);
      expect(m, `BridgeModule has no var at slot ${key} (__gap from Accounting)`)
        .to.not.equal(undefined);
      expect(m.label).to.equal(
        "__gap",
        `Slot ${key} is __gap on Accounting but ${m.label} on BridgeModule`,
      );
      expect(m.type).to.equal(
        g.type,
        `Type drift at slot ${key} for __gap (Accounting ${g.type} vs BridgeModule ${m.type})`,
      );
    }
  });

  it("LockModule storage layout matches Accounting (no slot drift on shared variables)", async () => {
    const acctLayout = await readStorageLayout("Accounting");
    const lockLayout = await readStorageLayout("LockModule");
    assertSharedPrefix(lockLayout, acctLayout);
  });

  it("__gap entries on Accounting are mirrored in LockModule (local gap-shrinkage gate)", async () => {
    // OZ's upgrade validator catches gap shrinkage on actual upgrades; this
    // is a faster local gate that runs every test cycle without spinning up
    // a proxy. Walk every `__gap` entry on Accounting and assert the same
    // {slot, offset, type} appears on LockModule's layout.
    const acctLayout = await readStorageLayout("Accounting");
    const moduleLayout = await readStorageLayout("LockModule");
    const moduleIndex = new Map<string, any>();
    for (const e of moduleLayout.storage as any[]) {
      moduleIndex.set(`${e.slot}:${e.offset}`, e);
    }
    const gaps = (acctLayout.storage as any[]).filter(
      (e) => e.label === "__gap",
    );
    expect(gaps.length).to.be.greaterThan(
      0,
      "Accounting has no __gap entries — refactor likely removed them",
    );
    for (const g of gaps) {
      const key = `${g.slot}:${g.offset}`;
      const m = moduleIndex.get(key);
      expect(m, `LockModule has no var at slot ${key} (__gap from Accounting)`)
        .to.not.equal(undefined);
      expect(m.label).to.equal(
        "__gap",
        `Slot ${key} is __gap on Accounting but ${m.label} on LockModule`,
      );
      expect(m.type).to.equal(
        g.type,
        `Type drift at slot ${key} for __gap (Accounting ${g.type} vs LockModule ${m.type})`,
      );
    }
  });
});
