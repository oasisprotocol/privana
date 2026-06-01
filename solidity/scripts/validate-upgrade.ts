import { ethers, upgrades } from 'hardhat';

async function main() {
  const siweAuthAddress = ethers.getAddress('0x0000000000000000000000000000000000000001');
  const accountingValidationOptions = {
    kind: 'uups' as const,
    unsafeAllow: ['constructor', 'state-variable-immutable', 'delegatecall'],
    constructorArgs: [siweAuthAddress],
  };
  // MockAccountingV2 adds a reinitializer-only upgrade, so it additionally needs
  // 'missing-initializer'; everything else matches the Accounting options.
  const mockV2UpgradeOptions = {
    ...accountingValidationOptions,
    unsafeAllow: [...accountingValidationOptions.unsafeAllow, 'missing-initializer'],
  };

  console.log('Validating fresh-layout implementation safety for MockAccounting...');

  const MockAccounting = await ethers.getContractFactory('MockAccounting');
  await upgrades.validateImplementation(MockAccounting, accountingValidationOptions);

  console.log('Validating fresh-layout implementation safety for Accounting...');

  const Accounting = await ethers.getContractFactory('Accounting');
  await upgrades.validateImplementation(Accounting, accountingValidationOptions);

  console.log('Validating fresh-layout implementation safety for MockAccountingSigner...');

  const MockAccountingSigner = await ethers.getContractFactory('MockAccountingSigner');
  await upgrades.validateImplementation(MockAccountingSigner, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
  });

  console.log('Validating fresh-layout implementation safety for AccountingSigner...');

  const AccountingSigner = await ethers.getContractFactory('AccountingSigner');
  await upgrades.validateImplementation(AccountingSigner, {
    kind: 'uups',
    unsafeAllow: ['constructor', 'state-variable-immutable'],
  });

  console.log('Validating fresh-layout Accounting upgrade safety...');

  const MockAccountingV2 = await ethers.getContractFactory('MockAccountingV2');
  await upgrades.validateUpgrade(
    MockAccounting,
    MockAccountingV2,
    mockV2UpgradeOptions
  );

  console.log('Confirming pre-split inline-signing layout is rejected...');

  const MockAccountingPrevious = await ethers.getContractFactory('MockAccountingPrevious');
  try {
    await upgrades.validateUpgrade(
      MockAccountingPrevious,
      MockAccounting,
      accountingValidationOptions
    );
    throw new Error('Expected pre-split inline-signing layout upgrade to be rejected');
  } catch (error) {
    const message = (error as Error).message;
    if (!/deleted|inserted|layout|upgrade safe/i.test(message)) {
      throw error;
    }
  }

  console.log('Fresh-layout validation passed; pre-split layout rejection confirmed');
}

main().catch((error) => {
  console.error('Fresh-layout validation failed:', error.message);
  process.exit(1);
});
