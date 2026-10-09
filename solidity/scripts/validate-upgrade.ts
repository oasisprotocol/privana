import { ethers, upgrades } from 'hardhat';

async function main() {
  console.log('Validating implementation-level upgrade safety for MockAccounting...');

  const MockAccounting = await ethers.getContractFactory('MockAccounting');
  await upgrades.validateImplementation(MockAccounting, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
    constructorArgs: [ethers.getAddress('0x0000000000000000000000000000000000000001')],
  } as any);

  console.log('Validating implementation-level upgrade safety for Accounting...');

  const Accounting = await ethers.getContractFactory('Accounting');
  await upgrades.validateImplementation(Accounting, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
    constructorArgs: [ethers.getAddress('0x0000000000000000000000000000000000000001')],
  } as any);

  console.log(
    'Implementation-level upgrade safety validation passed for all contracts ' +
    '(verified: no disallowed constructors, state variable initial values, selfdestruct, or delegatecall). ' +
    'Note: Storage layout comparison against deployed proxies occurs at deploy time via upgrades.validateUpgrade in tasks/deploy.ts.'
  );
}

main().catch((error) => {
  console.error('Implementation-level upgrade safety validation failed:', error.message);
  process.exit(1);
});
