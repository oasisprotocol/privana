import { expect } from 'chai';
import { artifacts, config, ethers, upgrades } from 'hardhat';
import { HardhatNetworkHDAccountsConfig } from 'hardhat/types';
import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import { Block, Wallet } from 'ethers';
import { AccountingHistoryModule, MockAccounting, MockSiweAuth } from '../typechain-types';
import { deployMockAccounting, mockAuthToken } from './utils';

async function isSapphireNetwork(): Promise<boolean> {
  const network = await ethers.provider.getNetwork();
  return network.chainId >= 0x5afd && network.chainId <= 0x5aff;
}

async function expectCustomErrorOrRevert(
  tx: Promise<unknown>,
  contract: any,
  errorName: string,
): Promise<void> {
  if (await isSapphireNetwork()) {
    await expect(tx).to.be.reverted;
  } else {
    await expect(tx).to.be.revertedWithCustomError(contract, errorName);
  }
}

async function expectReverted(call: Promise<unknown>): Promise<void> {
  try {
    await call;
  } catch {
    return;
  }
  expect.fail('Expected call to revert');
}

const types = {
  Lock: [
    { name: 'serviceAddress', type: 'address' },
    { name: 'tokenId', type: 'bytes32' },
    { name: 'amount', type: 'uint256' },
    { name: 'expiry', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
  ],
  ModifyLock: [
    { name: 'lockId', type: 'uint256' },
    { name: 'amount', type: 'uint256' },
    { name: 'newExpiry', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
  ],
  TransferLocked: [
    { name: 'userAddress', type: 'address' },
    { name: 'toAddress', type: 'address' },
    { name: 'lockId', type: 'uint256' },
    { name: 'amount', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
    { name: 'serviceAddress', type: 'address' },
  ],
  Transfer: [
    { name: 'toAddress', type: 'address' },
    { name: 'tokenId', type: 'bytes32' },
    { name: 'amount', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
  ],
  Withdraw: [
    { name: 'tokenId', type: 'bytes32' },
    { name: 'amount', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
  ],
  WithdrawFromLock: [
    { name: 'userAddress', type: 'address' },
    { name: 'toAddress', type: 'address' },
    { name: 'lockId', type: 'uint256' },
    { name: 'amount', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
  ],
};

const TEST_TOKEN = {
  tokenType: 1,
  tokenId: '0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514',
  chainId: 84532,
  address: '0x036cbd53842c5426634e7929541ec2318f3dcf7e',
};

const MOCK_ROFL_APP_ID = '0x' + '00'.repeat(21);
const abiCoder = ethers.AbiCoder.defaultAbiCoder();

function parseUsdt(amount: string): bigint {
  const [whole, fraction = ''] = amount.split('.');
  if (fraction.length > 6) {
    throw new Error('USDT supports up to 6 decimal places');
  }
  const wholePart = BigInt(whole) * BigInt(10 ** 6);
  const fractionPart = BigInt(fraction.padEnd(6, '0'));
  return wholePart + fractionPart;
}

function amountWord(amount: bigint): string {
  return ethers.zeroPadValue(ethers.toBeHex(amount), 32);
}

function depositPayload(tokenId: string, amount: bigint, depositId: string): string {
  return ethers.concat([tokenId, amountWord(amount), depositId]);
}

function counterpartyPayload(tokenId: string, amount: bigint, counterparty: string): string {
  return ethers.concat([tokenId, amountWord(amount), counterparty]);
}

function pairedTransferPayload(tokenId: string, amount: bigint, fromAddress: string, toAddress: string): string {
  return ethers.concat([tokenId, amountWord(amount), fromAddress, toAddress]);
}

function amountFromPayload(payload: string): bigint {
  return BigInt(ethers.dataSlice(payload, 32, 64));
}

function depositKey(label: string): string {
  return ethers.keccak256(ethers.toUtf8Bytes(label));
}

async function latestTimestamp(): Promise<number> {
  const block = await ethers.provider.getBlock('latest');
  return block!.timestamp;
}

type StorageLayoutEntry = {
  label: string;
  contract: string;
  type: string;
  slot: string;
};

type BuildInfoWithStorageLayout = {
  output: {
    contracts: Record<string, Record<string, { storageLayout: { storage: StorageLayoutEntry[] } }>>;
  };
};

async function storageEntry(
  sourceName: string,
  contractName: string,
  label: string,
  typeFragment?: string
): Promise<StorageLayoutEntry> {
  const buildInfo = await artifacts.getBuildInfo(`${sourceName}:${contractName}`) as BuildInfoWithStorageLayout | undefined;
  if (!buildInfo) {
    throw new Error(`Missing build info for ${sourceName}:${contractName}`);
  }
  const storage = buildInfo.output.contracts[sourceName][contractName].storageLayout.storage;
  const entry = storage.find(
    (entry) =>
      entry.label === label &&
      entry.contract === `${sourceName}:${contractName}` &&
      (!typeFragment || entry.type.includes(typeFragment))
  );
  if (!entry) {
    throw new Error(`Missing storage entry ${label} in ${sourceName}:${contractName}`);
  }
  return entry;
}

type AbiInput = { type: string };
type AbiFunctionFragment = { type: 'function'; name: string; inputs: AbiInput[] };

function isAbiFunction(fragment: unknown): fragment is AbiFunctionFragment {
  const candidate = fragment as Partial<AbiFunctionFragment>;
  return candidate.type === 'function' && typeof candidate.name === 'string' && Array.isArray(candidate.inputs);
}

describe('Accounting history', function () {
  let accounting: MockAccounting;
  let historyReader: AccountingHistoryModule;
  let historyModuleContract: AccountingHistoryModule;
  let mockSiweAuth: MockSiweAuth;
  let deployer: HardhatEthersSigner;
  let userWallet1: Wallet;
  let userWallet2: Wallet;
  let userWallet3: Wallet;
  let user1Signer: HardhatEthersSigner;
  let user2Signer: HardhatEthersSigner;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };

  beforeEach(async () => {
    [deployer] = await ethers.getSigners();
    const mnemonic = (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig).mnemonic;

    userWallet1 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/0")
      .connect(ethers.provider) as any;
    userWallet2 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/1")
      .connect(ethers.provider) as any;
    userWallet3 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/2")
      .connect(ethers.provider) as any;

    user1Signer = await ethers.getSigner(userWallet1.address);
    user2Signer = await ethers.getSigner(userWallet2.address);

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
    mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    accounting = await deployMockAccounting(await mockSiweAuth.getAddress());
    const AccountingHistoryModuleFactory = await ethers.getContractFactory('AccountingHistoryModule');
    historyReader = AccountingHistoryModuleFactory.attach(await accounting.getAddress()) as unknown as AccountingHistoryModule;
    historyModuleContract = AccountingHistoryModuleFactory.attach(await accounting.historyModule()) as unknown as AccountingHistoryModule;

    const data = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
      ethers.zeroPadValue(TEST_TOKEN.address, 20),
    ]);
    await accounting.setTokenInfo({ tokenType: TEST_TOKEN.tokenType, data });
    await accounting.setGasPrice(TEST_TOKEN.chainId, 1000000000n);

    const domainTuple = await accounting.eip712Domain();
    domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };
  });

  it('uses _authSender semantics for empty and non-empty tokens while isolating users', async function () {
    const depositTx1 = await accounting.mockCreditDeposit(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      depositKey('u1')
    );
    const depositReceipt1 = await depositTx1.wait();

    const depositTx2 = await accounting.mockCreditDeposit(userWallet2.address, TEST_TOKEN.tokenId, parseUsdt('2'), depositKey('u2'));
    await depositTx2.wait();

    const [callerHistory, callerTotal] = await historyReader.connect(user1Signer).getHistory(0, 10, '0x');
    const [emptyTokenUser2History, emptyTokenUser2Total] = await historyReader.connect(user2Signer).getHistory(0, 10, '0x');
    const [tokenHistory, tokenTotal] = await historyReader.getHistory(0, 10, mockAuthToken(userWallet2.address));

    expect(callerTotal).to.equal(1n);
    expect(emptyTokenUser2Total).to.equal(1n);
    expect(tokenTotal).to.equal(1n);
    expect(callerHistory[0].kind).to.equal(0n);
    expect(emptyTokenUser2History[0].kind).to.equal(0n);

    // Sapphire has the timestamp equal to the pre-last block. Other (non-L2) chains have the timestamp of the last block.
    const network = await ethers.provider.getNetwork();
    let depositBlock1: Block;
    if ((0x5afd <= network.chainId) && (network.chainId <= 0x5aff)) {
      depositBlock1 = (await ethers.provider.getBlock(depositReceipt1!.blockNumber - 1))!;
    } else {
      depositBlock1 = (await ethers.provider.getBlock(depositReceipt1!.blockNumber))!;
    }
    expect(callerHistory[0].timestamp).to.equal(BigInt(depositBlock1!.timestamp));

    expect(callerHistory[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('1'), depositKey('u1'))
    );
    expect(tokenHistory[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('2'), depositKey('u2'))
    );
    expect(emptyTokenUser2History[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('2'), depositKey('u2'))
    );

    await expectReverted(
      historyReader.getHistory(0, 10, mockAuthToken('0x0000000000000000000000000000000000000000'))
    );
  });

  it('rejects direct calls to the history module', async function () {
    await expect(historyModuleContract.getHistory(0, 10, mockAuthToken(userWallet1.address))).to.be.reverted;
  });

  it('can replace the history module without moving Accounting-owned history', async function () {
    await accounting.mockCreditDeposit(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      depositKey('before-module-replacement')
    );

    const AccountingHistoryModuleFactory = await ethers.getContractFactory('AccountingHistoryModule');
    const replacementModule = await AccountingHistoryModuleFactory.deploy();
    await replacementModule.waitForDeployment();

    const replacementAddress = await replacementModule.getAddress();
    const replacementTx = await accounting.setHistoryModule(replacementAddress);
    await replacementTx.wait();
    expect(await accounting.historyModule()).to.equal(replacementAddress);

    const [historyBefore, totalBefore] = await historyReader.getHistory(0, 10, mockAuthToken(userWallet1.address));
    expect(totalBefore).to.equal(1n);
    expect(historyBefore[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('1'), depositKey('before-module-replacement'))
    );

    await accounting.mockCreditDeposit(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt('2'),
      depositKey('after-module-replacement')
    );
    const [historyAfter, totalAfter] = await historyReader.getHistory(0, 10, mockAuthToken(userWallet1.address));
    expect(totalAfter).to.equal(2n);
    expect(historyAfter[1].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('2'), depositKey('after-module-replacement'))
    );
  });

  it('rejects invalid history module links and gates the setter to the owner', async function () {
    const MockAccountingFactory = await ethers.getContractFactory('MockAccounting', deployer);
    const unlinkedAccounting = await upgrades.deployProxy(
      MockAccountingFactory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: 'uups',
        initializer: 'initialize',
        constructorArgs: [await mockSiweAuth.getAddress()],
        unsafeAllow: ['constructor', 'state-variable-immutable', 'delegatecall'],
      }
    ) as unknown as MockAccounting;
    await unlinkedAccounting.waitForDeployment();

    const AccountingHistoryModuleFactory = await ethers.getContractFactory('AccountingHistoryModule');
    const validModule = await AccountingHistoryModuleFactory.deploy();
    await validModule.waitForDeployment();

    expect(await unlinkedAccounting.historyModule()).to.equal(ethers.ZeroAddress);
    await expect(unlinkedAccounting.setHistoryModule(userWallet1.address)).to.be.reverted;
    await expect(unlinkedAccounting.setHistoryModule(ethers.ZeroAddress)).to.be.reverted;
    await expectCustomErrorOrRevert(
      unlinkedAccounting.setHistoryModule(await mockSiweAuth.getAddress()),
      unlinkedAccounting,
      'InvalidHistoryModule'
    );
    await expect(
      unlinkedAccounting.connect(user2Signer).setHistoryModule(await validModule.getAddress())
    ).to.be.reverted;
    expect(await unlinkedAccounting.historyModule()).to.equal(ethers.ZeroAddress);

    const linkTx = await unlinkedAccounting.setHistoryModule(await validModule.getAddress());
    await linkTx.wait();
    expect(await unlinkedAccounting.historyModule()).to.equal(await validModule.getAddress());
  });

  it('rejects history modules that fail the delegated read smoke test', async function () {
    const MockBrokenHistoryModuleFactory = await ethers.getContractFactory('MockBrokenHistoryModule');
    const brokenModule = await MockBrokenHistoryModuleFactory.deploy();
    await brokenModule.waitForDeployment();

    await expectCustomErrorOrRevert(
      accounting.setHistoryModule(await brokenModule.getAddress()),
      accounting,
      'InvalidHistoryModule'
    );
  });

  it('keeps delegated history on Accounting-owned storage', async function () {
    const currentHistory = await storageEntry('contracts/Accounting.sol', 'Accounting', 'history');
    const currentHistoryModule = await storageEntry('contracts/Accounting.sol', 'Accounting', 'historyModule');
    const currentSigner = await storageEntry('contracts/Accounting.sol', 'Accounting', 'signer');
    const currentGap = await storageEntry('contracts/Accounting.sol', 'Accounting', '__gap', '39_storage');

    expect(currentHistory.slot).to.equal('58');
    expect(currentHistoryModule.slot).to.equal('59');
    expect(currentSigner.slot).to.equal('60');
    expect(currentGap.slot).to.equal('62');
  });

  it('exposes getHistory through the Accounting ABI', async function () {
    await accounting.mockCreditDeposit(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      depositKey('accounting-abi')
    );

    const accountingArtifact = await artifacts.readArtifact('Accounting');
    const selectors = new Set(
      accountingArtifact.abi
        .filter(isAbiFunction)
        .map((fragment) => {
          const inputTypes = fragment.inputs.map((input) => input.type).join(',');
          return ethers.id(`${fragment.name}(${inputTypes})`).slice(0, 10);
        })
    );

    expect(selectors.has(ethers.id('getHistory(int256,uint256,bytes)').slice(0, 10))).to.equal(true);

    const [page, total] = await accounting.getHistory.staticCall(
      0,
      10,
      mockAuthToken(userWallet1.address)
    );
    expect(total).to.equal(1n);
    expect(page[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('1'), depositKey('accounting-abi'))
    );
  });

  it('returns oldest-first pages and clamps limit to 100', async function () {
    const MockAccountingHelper = await ethers.getContractFactory('MockAccountingHelper');
    const mockAccountingHelper = await MockAccountingHelper.deploy(accounting);
    await mockAccountingHelper.waitForDeployment();

    const tx = await mockAccountingHelper.mockCreditDepositNTimes(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      1,
      depositKey('deposit-'),
      105
    );
    await tx.wait();

    const [page, total] = await historyReader.getHistory(0, 200, mockAuthToken(userWallet1.address));
    const [tailPage, tailTotal] = await historyReader.getHistory(10, 10, mockAuthToken(userWallet1.address));
    const [lastPage, lastTotal] = await historyReader.getHistory(-1, 10, mockAuthToken(userWallet1.address));
    const [preLastPage, preLastTotal] = await historyReader.getHistory(-2, 10, mockAuthToken(userWallet1.address));

    expect(total).to.equal(105n);
    expect(tailTotal).to.equal(105n);
    expect(lastTotal).to.equal(105n);
    expect(preLastTotal).to.equal(105n);
    expect(page.length).to.equal(100);
    expect(amountFromPayload(page[0].payload)).to.equal(1n);
    expect(amountFromPayload(page[99].payload)).to.equal(100n);
    expect(tailPage.length).to.equal(5);
    expect(amountFromPayload(tailPage[0].payload)).to.equal(101n);
    expect(amountFromPayload(tailPage[4].payload)).to.equal(105n);
    expect(lastPage.map((entry) => amountFromPayload(entry.payload))).to.deep.equal(
      tailPage.map((entry) => amountFromPayload(entry.payload))
    );
    expect(preLastPage.length).to.equal(10);
    expect(amountFromPayload(preLastPage[0].payload)).to.equal(91n);
    expect(amountFromPayload(preLastPage[9].payload)).to.equal(100n);
  });

  it('records fund movements and recipient mirrors', async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('50'));
    await accounting.mockCreditDeposit(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('3'), depositKey('seed'));

    const expiry = (await latestTimestamp()) + 3600;
    const createLockNonce = await accounting.createLockNonces(userWallet1.address);
    const createLockSignature = await userWallet1.signTypedData(domain, { Lock: types.Lock }, {
      serviceAddress: userWallet2.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('10'),
      expiry,
      nonce: createLockNonce,
    });
    await accounting.createLock(
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt('10'),
      expiry,
      createLockNonce,
      createLockSignature
    );

    const transferLockedNonce = await accounting.transferLockedNonces(userWallet2.address);
    const transferLockedSignature = await userWallet2.signTypedData(domain, { TransferLocked: types.TransferLocked }, {
      userAddress: userWallet1.address,
      toAddress: userWallet3.address,
      lockId: 1,
      amount: parseUsdt('4'),
      nonce: transferLockedNonce,
      serviceAddress: userWallet2.address,
    });
    await accounting.transferFromLock(
      userWallet1.address,
      userWallet3.address,
      1,
      parseUsdt('4'),
      transferLockedNonce,
      transferLockedSignature
    );

    const withdrawFromLockNonce = await accounting.withdrawFromLockNonces(userWallet2.address);
    const withdrawFromLockSignature = await userWallet2.signTypedData(domain, { WithdrawFromLock: types.WithdrawFromLock }, {
      userAddress: userWallet1.address,
      toAddress: userWallet3.address,
      lockId: 1,
      amount: parseUsdt('2'),
      nonce: withdrawFromLockNonce,
    });
    await accounting.withdrawFromLock(
      userWallet1.address,
      userWallet3.address,
      1,
      parseUsdt('2'),
      withdrawFromLockNonce,
      withdrawFromLockSignature
    );

    const transferNonce = await accounting.transferNonces(userWallet1.address);
    const transferSignature = await userWallet1.signTypedData(domain, { Transfer: types.Transfer }, {
      toAddress: userWallet3.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('1'),
      nonce: transferNonce,
    });
    await accounting.transferBalance(
      userWallet3.address,
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      transferNonce,
      transferSignature
    );

    const withdrawalNonce = await accounting.withdrawalNonces(userWallet1.address);
    const withdrawalSignature = await userWallet1.signTypedData(domain, { Withdraw: types.Withdraw }, {
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('1'),
      nonce: withdrawalNonce,
    });
    await accounting.requestWithdrawal(
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      withdrawalNonce,
      withdrawalSignature
    );

    const [user1History, user1Total] = await historyReader.getHistory(0, 20, mockAuthToken(userWallet1.address));
    const [user3History, user3Total] = await historyReader.getHistory(0, 20, mockAuthToken(userWallet3.address));

    expect(user1Total).to.equal(6n);
    expect(user1History.map((entry) => Number(entry.kind))).to.deep.equal([0, 2, 3, 1, 4, 1]);

    expect(user1History[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('3'), depositKey('seed'))
    );
    expect(user1History[1].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('10'), userWallet2.address)
    );
    expect(user1History[2].payload).to.equal(
      pairedTransferPayload(TEST_TOKEN.tokenId, parseUsdt('4'), userWallet1.address, userWallet3.address)
    );
    expect(user1History[3].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('2'), userWallet3.address)
    );
    expect(user1History[4].payload).to.equal(
      pairedTransferPayload(TEST_TOKEN.tokenId, parseUsdt('1'), userWallet1.address, userWallet3.address)
    );
    expect(user1History[5].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('1'), userWallet1.address)
    );

    expect(user3Total).to.equal(2n);
    expect(user3History.map((entry) => Number(entry.kind))).to.deep.equal([3, 4]);
    expect(user3History[0].payload).to.equal(
      pairedTransferPayload(TEST_TOKEN.tokenId, parseUsdt('4'), userWallet1.address, userWallet3.address)
    );
    expect(user3History[1].payload).to.equal(
      pairedTransferPayload(TEST_TOKEN.tokenId, parseUsdt('1'), userWallet1.address, userWallet3.address)
    );
  });

  it('records one row for self-directed paired transfers', async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('10'));

    const expiry = (await latestTimestamp()) + 3600;
    const createLockNonce = await accounting.createLockNonces(userWallet1.address);
    const createLockSignature = await userWallet1.signTypedData(domain, { Lock: types.Lock }, {
      serviceAddress: userWallet2.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('4'),
      expiry,
      nonce: createLockNonce,
    });
    await accounting.createLock(userWallet2.address, TEST_TOKEN.tokenId, parseUsdt('4'), expiry, createLockNonce, createLockSignature);

    const transferLockedNonce = await accounting.transferLockedNonces(userWallet2.address);
    const transferLockedSignature = await userWallet2.signTypedData(domain, { TransferLocked: types.TransferLocked }, {
      userAddress: userWallet1.address,
      toAddress: userWallet1.address,
      lockId: 1,
      amount: parseUsdt('1'),
      nonce: transferLockedNonce,
      serviceAddress: userWallet2.address,
    });
    await accounting.transferFromLock(
      userWallet1.address,
      userWallet1.address,
      1,
      parseUsdt('1'),
      transferLockedNonce,
      transferLockedSignature
    );

    const transferNonce = await accounting.transferNonces(userWallet1.address);
    const transferSignature = await userWallet1.signTypedData(domain, { Transfer: types.Transfer }, {
      toAddress: userWallet1.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('2'),
      nonce: transferNonce,
    });
    await accounting.transferBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('2'), transferNonce, transferSignature);

    const [history, total] = await historyReader.getHistory(0, 10, mockAuthToken(userWallet1.address));

    expect(total).to.equal(3n);
    expect(history.map((entry) => Number(entry.kind))).to.deep.equal([2, 3, 4]);
    expect(history[1].payload).to.equal(
      pairedTransferPayload(TEST_TOKEN.tokenId, parseUsdt('1'), userWallet1.address, userWallet1.address)
    );
    expect(history[2].payload).to.equal(
      pairedTransferPayload(TEST_TOKEN.tokenId, parseUsdt('2'), userWallet1.address, userWallet1.address)
    );
  });

  it('records only the sender row for zero-address paired transfer recipients', async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('10'));

    const transferNonce = await accounting.transferNonces(userWallet1.address);
    const transferSignature = await userWallet1.signTypedData(domain, { Transfer: types.Transfer }, {
      toAddress: ethers.ZeroAddress,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('2'),
      nonce: transferNonce,
    });
    await accounting.transferBalance(ethers.ZeroAddress, TEST_TOKEN.tokenId, parseUsdt('2'), transferNonce, transferSignature);

    const [history, total] = await historyReader.getHistory(0, 10, mockAuthToken(userWallet1.address));

    expect(total).to.equal(1n);
    expect(history[0].kind).to.equal(4n);
    expect(history[0].payload).to.equal(
      pairedTransferPayload(TEST_TOKEN.tokenId, parseUsdt('2'), userWallet1.address, ethers.ZeroAddress)
    );
  });

  it('records modifyLock history', async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('10'));
    const initialExpiry = (await latestTimestamp()) + 3600;

    const createLockNonce = await accounting.createLockNonces(userWallet1.address);
    const createLockSignature = await userWallet1.signTypedData(domain, { Lock: types.Lock }, {
      serviceAddress: userWallet2.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('3'),
      expiry: initialExpiry,
      nonce: createLockNonce,
    });
    await accounting.createLock(
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt('3'),
      initialExpiry,
      createLockNonce,
      createLockSignature
    );

    const modifyLockNonce = await accounting.modifyLockNonces(userWallet1.address);
    const modifyLockSignature = await userWallet1.signTypedData(domain, { ModifyLock: types.ModifyLock }, {
      lockId: 1,
      amount: parseUsdt('2'),
      newExpiry: initialExpiry + 300,
      nonce: modifyLockNonce,
    });
    await accounting.modifyLock(
      1,
      parseUsdt('2'),
      initialExpiry + 300,
      modifyLockNonce,
      modifyLockSignature
    );

    const expiryOnlyNonce = await accounting.modifyLockNonces(userWallet1.address);
    const expiryOnlySignature = await userWallet1.signTypedData(domain, { ModifyLock: types.ModifyLock }, {
      lockId: 1,
      amount: 0,
      newExpiry: initialExpiry + 600,
      nonce: expiryOnlyNonce,
    });
    await accounting.modifyLock(
      1,
      0,
      initialExpiry + 600,
      expiryOnlyNonce,
      expiryOnlySignature
    );

    const [history, total] = await historyReader.getHistory(0, 10, mockAuthToken(userWallet1.address));

    expect(total).to.equal(3n);
    expect(history.map((entry) => Number(entry.kind))).to.deep.equal([2, 5, 5]);
    expect(history[0].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('3'), userWallet2.address)
    );
    expect(history[1].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('2'), userWallet2.address)
    );
    expect(history[2].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, 0n, userWallet2.address)
    );
  });

  it('records single and batch unlock history', async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('30'));
    const expiry = (await latestTimestamp()) + 10;

    for (let lockIndex = 0; lockIndex < 3; lockIndex++) {
      const amount = parseUsdt(String(lockIndex + 1));
      const nonce = await accounting.createLockNonces(userWallet1.address);
      const signature = await userWallet1.signTypedData(domain, { Lock: types.Lock }, {
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount,
        expiry: expiry + lockIndex,
        nonce,
      });
      const tx = await accounting.createLock(
        userWallet2.address,
        TEST_TOKEN.tokenId,
        amount,
        expiry + lockIndex,
        nonce,
        signature
      );
      await tx.wait();
    }

    const [history1, total1] = await historyReader.getHistory(0, 20, mockAuthToken(userWallet1.address));
    expect(total1).to.equal(3n);
    expect(history1.map((entry) => Number(entry.kind))).to.deep.equal([2, 2, 2]);

    // Sapphire doesn't support evm_mine and evm_increaseTime.
    const network = await ethers.provider.getNetwork();
    if ((0x5afd <= network.chainId) && (network.chainId <= 0x5aff)) {
      this.skip();
    }

    await ethers.provider.send('evm_increaseTime', [3600]);
    await ethers.provider.send('evm_mine', []);

    const tx1 = await accounting.unlockSingleLock(userWallet1.address, 1);
    await tx1.wait();
    await expect(accounting.unlockAllExpiredLocks(userWallet1.address)).to.not.be.reverted;

    const [history2, total2] = await historyReader.getHistory(0, 20, mockAuthToken(userWallet1.address));

    expect(total2).to.equal(6n);
    expect(history2.map((entry) => Number(entry.kind))).to.deep.equal([2, 2, 2, 6, 6, 6]);
    expect(history2[3].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('1'), userWallet2.address)
    );
    expect(history2[4].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('2'), userWallet2.address)
    );
    expect(history2[5].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('3'), userWallet2.address)
    );
  });

  it('returns an empty page for out-of-range offsets while preserving total', async function () {
    for (let i = 1; i <= 3; i++) {
      await accounting.mockCreditDeposit(
        userWallet1.address,
        TEST_TOKEN.tokenId,
        BigInt(i),
        depositKey(`out-of-range-${i}`)
      );
    }

    // 3 entries, limit=10 -> pageCount=1; only page 0 exists.
    const [justPastEndPage, justPastEndTotal] = await historyReader.getHistory(1, 10, mockAuthToken(userWallet1.address));
    const [farPastEndPage, farPastEndTotal] = await historyReader.getHistory(10, 10, mockAuthToken(userWallet1.address));
    const [tooFarFromEndPage, tooFarFromEndTotal] = await historyReader.getHistory(-2, 10, mockAuthToken(userWallet1.address));

    expect(justPastEndTotal).to.equal(3n);
    expect(farPastEndTotal).to.equal(3n);
    expect(tooFarFromEndTotal).to.equal(3n);
    expect(justPastEndPage).to.have.length(0);
    expect(farPastEndPage).to.have.length(0);
    expect(tooFarFromEndPage).to.have.length(0);
  });

  it('returns an empty page when limit is zero', async function () {
    await accounting.mockCreditDeposit(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      depositKey('limit-zero')
    );

    const [page, total] = await historyReader.getHistory(0, 0, mockAuthToken(userWallet1.address));

    expect(total).to.equal(1n);
    expect(page).to.have.length(0);
  });

  it('rejects zero-amount direct withdrawals before appending history', async function () {
    const withdrawalNonce = await accounting.withdrawalNonces(userWallet1.address);
    const withdrawalSignature = await userWallet1.signTypedData(domain, { Withdraw: types.Withdraw }, {
      tokenId: TEST_TOKEN.tokenId,
      amount: 0,
      nonce: withdrawalNonce,
    });

    await expect(
      accounting.requestWithdrawal(
        TEST_TOKEN.tokenId,
        0,
        withdrawalNonce,
        withdrawalSignature
      )
    ).to.be.reverted; // WithCustomError(accounting, 'InvalidAmount'); // https://github.com/oasisprotocol/sapphire-paratime/issues/688

    const [history, total] = await historyReader.getHistory(0, 10, mockAuthToken(userWallet1.address));
    expect(total).to.equal(0n);
    expect(history).to.have.length(0);
  });
});
