import { artifacts } from 'hardhat';

// Accounting deploys to Oasis Sapphire, whose runtime raised the create-contract
// limit to 64 KiB (oasis-sdk#2471, Sapphire >= 1.3.0-testnet). Keep a 1 KiB
// safety buffer so nothing is ever deployed right at the hard ceiling.
const SAPPHIRE_LIMIT_BYTES = 65536;
const SIZE_BUFFER_BYTES = 1024;

async function checkContractSize(contractName: string, maxBytes: number): Promise<void> {
  const artifact = await artifacts.readArtifact(contractName);
  const deployedBytecode = artifact.deployedBytecode.startsWith('0x')
    ? artifact.deployedBytecode.slice(2)
    : artifact.deployedBytecode;
  const deployedSize = deployedBytecode.length / 2;

  console.log(`${contractName}: deployed bytecode size = ${deployedSize} bytes (limit ${maxBytes})`);

  if (deployedSize > maxBytes) {
    throw new Error(
      `${contractName} exceeds the Sapphire contract size budget by ${deployedSize - maxBytes} bytes (${deployedSize}/${maxBytes}).`
    );
  }
}

async function main() {
  await checkContractSize('Accounting', SAPPHIRE_LIMIT_BYTES - SIZE_BUFFER_BYTES);
  console.log('Bytecode size checks passed.');
}

main().catch((error) => {
  console.error('Bytecode size check failed:', error.message);
  process.exit(1);
});
