import { TypedDataDomain } from 'ethers';

/**
 * Builds the EIP-712 signing domain from a contract's ERC-5267 report,
 * including only the fields its bitmap declares. ethers derives the
 * EIP712Domain type from the keys present on the object, so a field the
 * contract omits (chainId, whose place the salt takes) must not appear here
 * at all — an explicit `chainId: 0` would hash a different domain separator.
 */
export async function eip712DomainOf(contract: {
	eip712Domain(): Promise<{ [index: number]: unknown }>;
}): Promise<TypedDataDomain> {
	const domain = await contract.eip712Domain();
	const bitmap = Number(domain[0]);
	const built: TypedDataDomain = {};
	if (bitmap & 0x01) built.name = String(domain[1]);
	if (bitmap & 0x02) built.version = String(domain[2]);
	if (bitmap & 0x04) built.chainId = Number(domain[3]);
	if (bitmap & 0x08) built.verifyingContract = String(domain[4]);
	if (bitmap & 0x10) built.salt = String(domain[5]);
	return built;
}
