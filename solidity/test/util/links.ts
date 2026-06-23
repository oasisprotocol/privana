import { ethers } from "hardhat";
import type { Contract, Signer } from "ethers";

/// Binds the Accounting ABI to a deployed proxy address. A single handle
/// exposes the full surface, plus any mock-only helpers declared on `name`
/// (e.g. `MockAccountingBridgeExposure`).
export async function attachAccounting(
  proxyAddr: string,
  signer?: Signer,
  name: string = "MockAccounting",
): Promise<Contract> {
  return (await ethers.getContractAt(
    name,
    proxyAddr,
    signer,
  )) as unknown as Contract;
}
