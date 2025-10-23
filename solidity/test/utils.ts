
import { ethers } from 'hardhat';
import { Signer, BigNumberish, Interface, parseUnits, Wallet, type BytesLike, JsonRpcProvider, JsonRpcApiProvider } from 'ethers';

import { Trie } from "@ethereumjs/trie";
import { encode } from "rlp";

/**
 * Generates a legacy (pre-EIP-1559) native ETH transfer transaction, signed by the given signer.
 * @param signer Wallet or Signer to sign the transaction
 * @param to Recipient address
 * @param amount Amount in wei (BigNumberish)
 * @param chainId Chain ID
 * @param nonce Optional nonce (if not provided, will fetch from provider)
 * @returns Promise<SerializedTxHex>
 */
export async function generateNativeTx({
	signer,
	to,
	amount,
	chainId,
	nonce,
	type,
}: {
	signer: Wallet,
	to: string,
	amount: BigNumberish,
	chainId: number,
	nonce: number,
	type: number,
}): Promise<string> {

	const tx = {
		to,
		value: amount,
		nonce,
		gasLimit: 21000,
		gasPrice: parseUnits('1', 'gwei'),
		data: '0x',
		chainId,
		type // Transaction type
	};
	const signedTx = await signer.signTransaction(tx);
	return signedTx;
}

/**
 * Generates a legacy (pre-EIP-1559) ERC20 transfer transaction, signed by the given signer.
 * @param signer Wallet or Signer to sign the transaction
 * @param tokenAddress ERC20 contract address
 * @param to Recipient address
 * @param amount Amount in token's smallest unit (BigNumberish)
 * @param chainId Chain ID
 * @param nonce Optional nonce (if not provided, will fetch from provider)
 * @returns Promise<SerializedTxHex>
 */
export async function generateERC20Tx({
	signer,
	tokenAddress,
	to,
	amount,
	chainId,
	nonce,
	type,
}: {
	signer: Wallet,
	tokenAddress: string,
	to: string,
	amount: BigNumberish,
	chainId: number,
	nonce: number,
	type: number,
}): Promise<string> {
	// ERC20 transfer selector: a9059cbb
	const iface = new Interface([
		'function transfer(address to, uint256 amount)'
	]);
	const data = iface.encodeFunctionData('transfer', [to, amount]);
	const tx = {
		to: tokenAddress,
		value: 0,
		nonce,
		gasLimit: 60000,
		gasPrice: parseUnits('1', 'gwei'),
		data,
		chainId,
		type // Transaction type
	};
	const signedTx = await signer.signTransaction(tx);
	return signedTx;
}

// Inclusion proofs


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
