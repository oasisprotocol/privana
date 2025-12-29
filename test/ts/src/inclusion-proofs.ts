import {
  ethers,
  type BytesLike,
  JsonRpcApiProvider,
} from "ethers";
import { Trie } from "@ethereumjs/trie";
import { encode } from "rlp";
import assert from "assert";


export function getMappingStorageSlot(mappingKey: BytesLike, mappingSlot: BytesLike): string {
  return ethers.keccak256(
    ethers.concat([ethers.zeroPadValue(mappingKey, 32), ethers.zeroPadValue(mappingSlot, 32)]),
  );
}

export function getRpcUint(number: any): string {
  return "0x" + BigInt(number).toString(16);
}

export function getRlpUint(number: any): string {
  let hex_value = "0x" + number.toString(16);
  if (hex_value.length % 2 != 0) {
    hex_value = "0x0" + hex_value.slice(2);
  }
  return number > 0 ? ethers.encodeRlp(ethers.getBytes(hex_value)) : ethers.encodeRlp("0x");
}

export async function getTxInclusionProof(
  provider: JsonRpcApiProvider,
  blockNumber: number,
  txIndex: number,
): Promise<{ rlpBlockHeader: string; proof: string[] }> {
  const rawBlock = await provider.send("debug_getRawBlock", [getRpcUint(blockNumber)]);
  const blockRlp = ethers.decodeRlp(rawBlock);
  // TODO: Review whether we can make this type conversion
  const blockHeader: string[] = blockRlp[0] as string[];
  const rawTransactions: string[] = blockRlp[1] as string[];

  // Build Merkle tree
  const trie = new Trie();
  for (let [i, rawTransaction] of rawTransactions.entries()) {
    if (typeof rawTransaction == "object") {
      await trie.put(ethers.getBytes(getRlpUint(i)), ethers.getBytes(encode(rawTransaction)));
    } else {
      await trie.put(ethers.getBytes(getRlpUint(i)), ethers.getBytes(rawTransaction));
    }
  }

  // Ensure the transaction root was constructed the same way
  const txRoot = ethers.hexlify(trie.root());
  if (txRoot != blockHeader[4]) {
    throw new Error(
      "Constructed transaction Merkle tree has a root inconsistent with the transactionsRoot in the block header",
    );
  }

  if (txIndex >= rawTransactions.length) {
    throw new Error("Transaction index is outside the range of this block");
  }

  // Generate the proof of the transaction
  const txProof = await trie.createProof(ethers.getBytes(getRlpUint(txIndex)));
  const txProofHex = txProof.map((x) => ethers.hexlify(x));
  return {
    rlpBlockHeader: ethers.encodeRlp(blockHeader),
    proof: txProofHex,
  };
}


export async function getReceiptInclusionProof(
  provider: JsonRpcApiProvider,
  blockNumber: number,
  txIndex: number,
) {
  const receipts = await provider.send("eth_getBlockReceipts", [getRpcUint(blockNumber)]);

  const trie = new Trie();
  for (let i = 0; i < receipts.length; i++) {
    const key = ethers.getBytes(getRlpUint(i));   // RLP(k)
    await trie.put(key, arrayifyReceipt(receipts[i]));
  }

  const rawBlock = await provider.send("debug_getRawBlock", [getRpcUint(blockNumber)]);
  const blockRlp = ethers.decodeRlp(rawBlock);
  const blockHeader: string[] = blockRlp[0] as string[];

  // Ensure the transaction root was constructed the same way
  const txRoot = ethers.hexlify(trie.root());
  if (txRoot != blockHeader[5]) {
    throw new Error(
      "Constructed receipts Merkle tree has a root inconsistent with the receiptsRoot in the block header",
    );
  }

  //Generate the proof of the receipt
  const receiptsProof = await trie.createProof(ethers.getBytes(getRlpUint(txIndex)));
  const receiptsProofHex = receiptsProof.map((x) => ethers.hexlify(x));
  return {
    rlpBlockHeader: ethers.encodeRlp(blockHeader),
    proof: receiptsProofHex,
  };
}

function arrayifyReceipt(receipt: any): any {
  const status = typeof receipt.status === "string"
    ? parseInt(receipt.status, 16)
    : receipt.status ?? 0;

  const receiptArray = [
    status,                                    // Rz
    ethers.getUint(receipt.cumulativeGasUsed),           // Ru
    ethers.getBytes(receipt.logsBloom),        // Rb (256 bytes)
  ];

  const logsArray: any[] = [];
  for (const log of receipt.logs ?? []) {
    const logArray = [
      ethers.getAddress(log.address),
      (log.topics ?? []).map((t: any) => ethers.getBytes(t)),
      ethers.getBytes(log.data ?? "0x"),
    ];
    logsArray.push(logArray);                  // dense, in-order
  }

  receiptArray.push(logsArray);               // Rl

  const encoded = encode(receiptArray);

  // Prepend type byte if typed receipt
  const typeNum = typeof receipt.type === "string"
    ? parseInt(receipt.type, 16)
    : Number(receipt.type ?? 0);

  const isTyped = !Number.isNaN(typeNum) && typeNum !== 0;
  const encodedWithType = isTyped
    ? ethers.concat([Uint8Array.of(typeNum), encoded])
    : encoded;

  return encodedWithType;
}