import { ethers, upgrades } from 'hardhat';

async function main() {
  console.log('Validating upgrade safety for MockAccounting...');

  const MockAccounting = await ethers.getContractFactory('MockAccounting');
  await upgrades.validateImplementation(MockAccounting, { kind: 'uups' });

  console.log('Validating upgrade safety for Accounting...');

  const Accounting = await ethers.getContractFactory('Accounting');
  await upgrades.validateImplementation(Accounting, { kind: 'uups' });

  console.log('Storage layout validation passed for all contracts');
}

main().catch((error) => {
  console.error('Storage layout validation failed:', error.message);
  process.exit(1);
});
