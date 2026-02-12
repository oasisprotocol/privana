import { ethers, upgrades } from 'hardhat';

async function main() {
  console.log('Validating upgrade safety for MockAccounting...');

  const MockAccounting = await ethers.getContractFactory('MockAccounting');
  await upgrades.validateImplementation(MockAccounting, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
    constructorArgs: [ethers.getAddress('0x0000000000000000000000000000000000000001')],
  } as any);

  console.log('Validating upgrade safety for Accounting...');

  const Accounting = await ethers.getContractFactory('Accounting');
  await upgrades.validateImplementation(Accounting, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
    constructorArgs: [ethers.getAddress('0x0000000000000000000000000000000000000001')],
  } as any);

  console.log('Storage layout validation passed for all contracts');
}

main().catch((error) => {
  console.error('Storage layout validation failed:', error.message);
  process.exit(1);
});
