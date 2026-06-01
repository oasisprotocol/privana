import { artifacts } from 'hardhat';

const EIP170_LIMIT_BYTES = 24576;

async function checkContractSize(contractName: string, maxBytes: number): Promise<void> {
  const artifact = await artifacts.readArtifact(contractName);
  const deployedBytecode = artifact.deployedBytecode.startsWith('0x')
    ? artifact.deployedBytecode.slice(2)
    : artifact.deployedBytecode;
  const deployedSize = deployedBytecode.length / 2;

  console.log(`${contractName}: deployed bytecode size = ${deployedSize} bytes (limit ${maxBytes})`);

  if (deployedSize > maxBytes) {
    throw new Error(
      `${contractName} exceeds configured bytecode limit by ${deployedSize - maxBytes} bytes (${deployedSize}/${maxBytes}).`
    );
  }
}

async function main() {
  await checkContractSize('Accounting', EIP170_LIMIT_BYTES);
  await checkContractSize('AccountingSigner', EIP170_LIMIT_BYTES);
  await checkContractSize('AccountingHistoryModule', EIP170_LIMIT_BYTES);
  console.log('Bytecode size checks passed.');
}

main().catch((error) => {
  console.error('Bytecode size check failed:', error.message);
  process.exit(1);
});
