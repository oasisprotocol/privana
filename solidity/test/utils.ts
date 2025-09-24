
import { ethers } from 'hardhat';
import { Signer, BigNumberish, Interface, parseUnits, Wallet } from 'ethers';

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
