import { expect } from 'chai';
import { config, ethers, upgrades } from 'hardhat';
import { HardhatNetworkHDAccountsConfig } from 'hardhat/types';
import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import { Wallet } from 'ethers';
import { MockAccounting, MockSiweAuth } from '../typechain-types';

const types = {
  Lock: [
    { name: 'userAddress', type: 'address' },
    { name: 'serviceAddress', type: 'address' },
    { name: 'tokenId', type: 'bytes32' },
    { name: 'amount', type: 'uint256' },
    { name: 'expiry', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
  ],
  ModifyLock: [
    { name: 'userAddress', type: 'address' },
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
    { name: 'userAddress', type: 'address' },
    { name: 'toAddress', type: 'address' },
    { name: 'tokenId', type: 'bytes32' },
    { name: 'amount', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
  ],
  Withdraw: [
    { name: 'userAddress', type: 'address' },
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

function encodeSiweToken(address: string): string {
  return abiCoder.encode(['address'], [address]);
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

describe('Accounting history', function () {
  let accounting: MockAccounting;
  let mockSiweAuth: MockSiweAuth;
  let userWallet1: Wallet;
  let userWallet2: Wallet;
  let userWallet3: Wallet;
  let user1Signer: HardhatEthersSigner;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };

  beforeEach(async () => {
    const [deployer] = await ethers.getSigners();
    const mnemonic = (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig).mnemonic;

    userWallet1 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/0")
      .connect(ethers.provider);
    userWallet2 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/1")
      .connect(ethers.provider);
    userWallet3 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/2")
      .connect(ethers.provider);

    user1Signer = await ethers.getSigner(userWallet1.address);

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
    mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    const AccountingFactory = await ethers.getContractFactory('MockAccounting');
    accounting = await upgrades.deployProxy(
      AccountingFactory,
      [MOCK_ROFL_APP_ID, deployer.address],
      { kind: 'uups', initializer: 'initialize', constructorArgs: [await mockSiweAuth.getAddress()] }
    ) as unknown as MockAccounting;
    await accounting.waitForDeployment();

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
    const depositTx = await accounting.mockCreditDeposit(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      depositKey('u1')
    );
    const depositReceipt = await depositTx.wait();
    const depositBlock = await ethers.provider.getBlock(depositReceipt!.blockNumber);
    await accounting.mockCreditDeposit(userWallet2.address, TEST_TOKEN.tokenId, parseUsdt('2'), depositKey('u2'));

    const [callerHistory, callerTotal] = await accounting.connect(user1Signer).getHistory(0, 10, '0x');
    const [tokenHistory, tokenTotal] = await accounting.getHistory(0, 10, encodeSiweToken(userWallet2.address));

    expect(callerTotal).to.equal(1n);
    expect(tokenTotal).to.equal(1n);
    expect(callerHistory[0].kind).to.equal(0n);
    expect(callerHistory[0].timestamp).to.equal(BigInt(depositBlock!.timestamp));
    expect(callerHistory[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('1'), depositKey('u1'))
    );
    expect(tokenHistory[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('2'), depositKey('u2'))
    );

    await expect(
      accounting.connect(user1Signer).getHistory(0, 10, '0x12')
    ).to.be.reverted;
  });

  it('returns oldest-first pages and clamps limit to 100', async function () {
    for (let i = 1; i <= 105; i++) {
      await accounting.mockCreditDeposit(
        userWallet1.address,
        TEST_TOKEN.tokenId,
        BigInt(i),
        depositKey(`deposit-${i}`)
      );
    }

    const [page, total] = await accounting.connect(user1Signer).getHistory(0, 200, '0x');
    const [tailPage, tailTotal] = await accounting.connect(user1Signer).getHistory(10, 10, '0x');
    const [lastPage, lastTotal] = await accounting.connect(user1Signer).getHistory(-1, 10, '0x');
    const [preLastPage, preLastTotal] = await accounting.connect(user1Signer).getHistory(-2, 10, '0x');

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

  it('records only the canonical five kinds with payload-based metadata and no recipient mirrors', async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('50'));
    await accounting.mockCreditDeposit(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('3'), depositKey('seed'));

    const expiry = (await latestTimestamp()) + 3600;
    const createLockNonce = await accounting.createLockNonces(userWallet1.address);
    const createLockSignature = await userWallet1.signTypedData(domain, { Lock: types.Lock }, {
      userAddress: userWallet1.address,
      serviceAddress: userWallet2.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('10'),
      expiry,
      nonce: createLockNonce,
    });
    await accounting.createLock(
      userWallet1.address,
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
      userAddress: userWallet1.address,
      toAddress: userWallet3.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('1'),
      nonce: transferNonce,
    });
    await accounting.transferBalance(
      userWallet1.address,
      userWallet3.address,
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      transferNonce,
      transferSignature
    );

    const withdrawalNonce = await accounting.withdrawalNonces(userWallet1.address);
    const withdrawalSignature = await userWallet1.signTypedData(domain, { Withdraw: types.Withdraw }, {
      userAddress: userWallet1.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('1'),
      nonce: withdrawalNonce,
    });
    await accounting.requestWithdrawal(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt('1'),
      withdrawalNonce,
      withdrawalSignature
    );

    const [user1History, user1Total] = await accounting.getHistory(0, 20, encodeSiweToken(userWallet1.address));
    const [user3History, user3Total] = await accounting.getHistory(0, 20, encodeSiweToken(userWallet3.address));

    expect(user1Total).to.equal(6n);
    expect(user1History.map((entry) => Number(entry.kind))).to.deep.equal([0, 2, 3, 1, 4, 1]);

    expect(user1History[0].payload).to.equal(
      depositPayload(TEST_TOKEN.tokenId, parseUsdt('3'), depositKey('seed'))
    );
    expect(user1History[1].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('10'), userWallet2.address)
    );
    expect(user1History[2].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('4'), userWallet3.address)
    );
    expect(user1History[3].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('2'), userWallet3.address)
    );
    expect(user1History[4].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('1'), userWallet3.address)
    );
    expect(user1History[5].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('1'), userWallet1.address)
    );

    expect(user3Total).to.equal(0n);
    expect(user3History).to.have.length(0);
  });

  it('does not append history for modifyLock', async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('10'));
    const initialExpiry = (await latestTimestamp()) + 3600;

    const createLockNonce = await accounting.createLockNonces(userWallet1.address);
    const createLockSignature = await userWallet1.signTypedData(domain, { Lock: types.Lock }, {
      userAddress: userWallet1.address,
      serviceAddress: userWallet2.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: parseUsdt('3'),
      expiry: initialExpiry,
      nonce: createLockNonce,
    });
    await accounting.createLock(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt('3'),
      initialExpiry,
      createLockNonce,
      createLockSignature
    );

    const modifyLockNonce = await accounting.modifyLockNonces(userWallet1.address);
    const modifyLockSignature = await userWallet1.signTypedData(domain, { ModifyLock: types.ModifyLock }, {
      userAddress: userWallet1.address,
      lockId: 1,
      amount: parseUsdt('2'),
      newExpiry: initialExpiry + 300,
      nonce: modifyLockNonce,
    });
    await accounting.modifyLock(
      userWallet1.address,
      1,
      parseUsdt('2'),
      initialExpiry + 300,
      modifyLockNonce,
      modifyLockSignature
    );

    const [history, total] = await accounting.getHistory(0, 10, encodeSiweToken(userWallet1.address));

    expect(total).to.equal(1n);
    expect(Number(history[0].kind)).to.equal(2);
    expect(history[0].payload).to.equal(
      counterpartyPayload(TEST_TOKEN.tokenId, parseUsdt('3'), userWallet2.address)
    );
  });

  it('does not append history for single or batch unlocks', async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt('30'));
    const expiry = (await latestTimestamp()) + 10;

    for (let lockIndex = 0; lockIndex < 3; lockIndex++) {
      const nonce = await accounting.createLockNonces(userWallet1.address);
      const signature = await userWallet1.signTypedData(domain, { Lock: types.Lock }, {
        userAddress: userWallet1.address,
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt('1'),
        expiry: expiry + lockIndex,
        nonce,
      });
      await accounting.createLock(
        userWallet1.address,
        userWallet2.address,
        TEST_TOKEN.tokenId,
        parseUsdt('1'),
        expiry + lockIndex,
        nonce,
        signature
      );
    }

    await ethers.provider.send('evm_increaseTime', [3600]);
    await ethers.provider.send('evm_mine', []);

    await accounting.unlockSingleLock(userWallet1.address, 1);
    await accounting.unlockAllExpiredLocks(userWallet1.address);

    const [history, total] = await accounting.getHistory(0, 20, encodeSiweToken(userWallet1.address));

    expect(total).to.equal(3n);
    expect(history.map((entry) => Number(entry.kind))).to.deep.equal([2, 2, 2]);
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
    const [justPastEndPage, justPastEndTotal] = await accounting.connect(user1Signer).getHistory(1, 10, '0x');
    const [farPastEndPage, farPastEndTotal] = await accounting.connect(user1Signer).getHistory(10, 10, '0x');
    const [tooFarFromEndPage, tooFarFromEndTotal] = await accounting.connect(user1Signer).getHistory(-2, 10, '0x');

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

    const [page, total] = await accounting.connect(user1Signer).getHistory(0, 0, '0x');

    expect(total).to.equal(1n);
    expect(page).to.have.length(0);
  });

  it('rejects zero-amount direct withdrawals before appending history', async function () {
    const withdrawalNonce = await accounting.withdrawalNonces(userWallet1.address);
    const withdrawalSignature = await userWallet1.signTypedData(domain, { Withdraw: types.Withdraw }, {
      userAddress: userWallet1.address,
      tokenId: TEST_TOKEN.tokenId,
      amount: 0,
      nonce: withdrawalNonce,
    });

    await expect(
      accounting.requestWithdrawal(
        userWallet1.address,
        TEST_TOKEN.tokenId,
        0,
        withdrawalNonce,
        withdrawalSignature
      )
    ).to.be.revertedWithCustomError(accounting, 'InvalidAmount');

    const [history, total] = await accounting.getHistory(0, 10, encodeSiweToken(userWallet1.address));
    expect(total).to.equal(0n);
    expect(history).to.have.length(0);
  });
});
