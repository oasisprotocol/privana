import { ethers, upgrades } from 'hardhat';

async function main() {
  const siweAuthAddress = ethers.getAddress(
    '0x0000000000000000000000000000000000000001'
  );

  console.log(
    'Validating Accounting storage layout against previous inline-history layout...'
  );

  const MockAccountingPrevious = await ethers.getContractFactory(
    'MockAccountingPrevious'
  );
  const MockAccounting = await ethers.getContractFactory('MockAccounting');
  await upgrades.validateUpgrade(MockAccountingPrevious, MockAccounting, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
    constructorArgs: [siweAuthAddress],
  } as any);

  console.log('Validating upgrade safety for MockAccounting...');

  await upgrades.validateImplementation(MockAccounting, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
    constructorArgs: [siweAuthAddress],
  } as any);

  console.log('Validating upgrade safety for Accounting...');

  const Accounting = await ethers.getContractFactory('Accounting');
  await upgrades.validateImplementation(Accounting, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
    constructorArgs: [siweAuthAddress],
  } as any);

  console.log('Validating upgrade safety for AccountingHistory...');

  const AccountingHistory =
    await ethers.getContractFactory('AccountingHistory');
  await upgrades.validateImplementation(AccountingHistory, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
    constructorArgs: [siweAuthAddress],
  } as any);

  console.log('Storage layout validation passed for all contracts');
}

main().catch((error) => {
  console.error('Storage layout validation failed:', error.message);
  process.exit(1);
});
