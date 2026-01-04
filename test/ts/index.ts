import { ethers } from "ethers";
import { getReceiptInclusionProof, getTxInclusionProof } from "./src/inclusion-proofs";

async function main() {
  const RPC_URL = process.env.RPC_URL || "https://eth1.lava.build";
  const BLOCK_NUMBER = parseInt(process.env.BLOCK_NUMBER || "23488800");
  const TX_INDEX = parseInt(process.env.TX_INDEX || "1");

  const provider = new ethers.JsonRpcProvider(RPC_URL);

  try {
    const result = await getReceiptInclusionProof(provider, BLOCK_NUMBER, TX_INDEX);

    // console.log("RLP Block Header:", result.rlpBlockHeader);
    // console.log("Proof:", result.proof);
  } catch (error) {
    console.error("Error:", error);
    process.exit(1);
  }
}

main();
