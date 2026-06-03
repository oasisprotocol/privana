// Installs the canonical CreateX factory at `0xba5Ed099...` on the Hardhat
// Network via `hardhat_setCode`. The runtime bytecode is vendored at
// `solidity/test/fixtures/createx-runtime.json`; this helper verifies the
// embedded sha256 before injection so tampering with the fixture trips loudly.
//
// The fixture stores the **post-constructor** runtime — the bytes you'd read
// via `eth_getCode` on a chain where CreateX is live, not the artifact's
// `deployedBytecode` field. CreateX has `address internal immutable _SELF =
// address(this);` whose value is patched into the runtime by the constructor.
// `hardhat_setCode` skips the constructor entirely, so using the artifact's
// template runtime would leave `_SELF == address(0)` and `deployCreate3` would
// revert with `FailedContractCreation` against an off-by-_SELF predicted
// address. The fixture was cross-checked across Base Sepolia and Sapphire
// testnet (identical sha256) to confirm provenance.
//
// The helper is a no-op on networks that don't expose `hardhat_setCode`.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { ethers, network } from "hardhat";

import { CREATEX_ADDRESS } from "../../tasks/deploy-bridge";

interface CreateXFixture {
  source: string;
  tag: string;
  address: string;
  sha256: string;
  runtimeBytecode: string;
}

export async function installCreateX(): Promise<void> {
  if (network.name !== "hardhat") return;

  const fixturePath = join(__dirname, "..", "fixtures", "createx-runtime.json");
  const fixture = JSON.parse(
    readFileSync(fixturePath, "utf8"),
  ) as CreateXFixture;

  if (
    ethers.getAddress(fixture.address) !== ethers.getAddress(CREATEX_ADDRESS)
  ) {
    throw new Error(
      `installCreateX: fixture address ${fixture.address} != CREATEX_ADDRESS ${CREATEX_ADDRESS}`,
    );
  }

  const bytes = ethers.getBytes(fixture.runtimeBytecode);
  const actualSha = createHash("sha256").update(bytes).digest("hex");
  if (actualSha !== fixture.sha256) {
    throw new Error(
      `installCreateX: createx-runtime.json sha256 drift — expected ${fixture.sha256}, got ${actualSha}. ` +
        `Re-vendor from ${fixture.source} (tag ${fixture.tag}) or update the pin.`,
    );
  }

  await network.provider.send("hardhat_setCode", [
    CREATEX_ADDRESS,
    fixture.runtimeBytecode,
  ]);
}
