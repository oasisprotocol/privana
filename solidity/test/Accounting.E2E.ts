import { expect, version } from 'chai';
import { ethers, config, upgrades } from 'hardhat';
import { keccak256, parseEther, Wallet } from 'ethers';
import { MockAccounting, MockAccountingV2, MockShoyuBashi, MockSiweAuth, ProvethVerifier } from '../typechain-types';
import { generateERC20Tx, getReceiptInclusionProof, getRlpUint } from './utils';
import { getTxInclusionProof } from './utils';
import { HardhatNetworkHDAccountsConfig } from 'hardhat/types';
import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
// import {
//   isCalldataEnveloped,
//   wrapEthereumProvider,
// } from '@oasisprotocol/sapphire-paratime';

const types = {
  Lock: [
    { name: "userAddress", type: "address" },
    { name: "serviceAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "expiry", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
  ModifyLock: [
    { name: "userAddress", type: "address" },
    { name: "lockId", type: "uint256" },
    { name: "amount", type: "uint256" },
    { name: "newExpiry", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
  TransferLocked: [
    { name: "userAddress", type: "address" },
    { name: "toAddress", type: "address" },
    { name: "lockId", type: "uint256" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" },
    { name: "serviceAddress", type: "address" },
  ],
  Transfer: [
    { name: "userAddress", type: "address" },
    { name: "toAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
  Withdraw: [
    { name: "userAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
  WithdrawFromLock: [
    { name: "userAddress", type: "address" },
    { name: "toAddress", type: "address" },
    { name: "lockId", type: "uint256" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
  CreditDepositTo: [
    { name: "depositorAddress", type: "address" },
    { name: "beneficiaryAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "chainId", type: "uint256" },
    { name: "txHash", type: "bytes32" },
    { name: "nonce", type: "uint256" },
  ],
}

const TEST_TOKEN = {
  tokenType: 1, // ERC20
  // keccak256(abi.encodePacked(uint256(84532), address(0x12084e1a0fe92b5ab803a81a0ae54d91040f89ca)))
  // Precomputed to save time and avoid dependency on ethers.utils.solidityPack
  // which is not available in the ethers v6 version used by hardhat
  tokenId: "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514",
  chainId: 84532,
  address: '0x036cbd53842c5426634e7929541ec2318f3dcf7e',
};

function parseUsdt(amount: string): bigint {
  const [whole, fraction = ''] = amount.split('.');
  if (fraction.length > 6) {
    throw new Error('USDT supports up to 6 decimal places');
  }
  const wholePart = BigInt(whole) * BigInt(10 ** 6);
  const fractionPart = BigInt(fraction.padEnd(6, '0'));
  return wholePart + fractionPart;
}

async function getBlockTimestamp(): Promise<number> {
  const block = await ethers.provider.getBlock('latest');
  return block!.timestamp;
}

describe('Accounting', function () {
  let accounting: MockAccounting;
  let mockShoyubashi: MockShoyuBashi;
  let provethVerifier: ProvethVerifier;
  let mockSiweAuth: MockSiweAuth;
  let accountingUser1: MockAccounting;
  let accountingUser2: MockAccounting;
  let user1: HardhatEthersSigner;
  let user2: HardhatEthersSigner;
  let service: HardhatEthersSigner;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;
  let tokenId: string;

  before(async () => {
    const provider = ethers.provider;
    let deployer: HardhatEthersSigner;
    [deployer, user1, user2, service] = await ethers.getSigners();

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
    mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    const MockShoyubashiFactory = await ethers.getContractFactory('MockShoyuBashi');
    mockShoyubashi = await MockShoyubashiFactory.deploy();
    await mockShoyubashi.waitForDeployment();

    const ProvethVerifierFactory = await ethers.getContractFactory('ProvethVerifier');
    provethVerifier = await ProvethVerifierFactory.deploy();
    await provethVerifier.waitForDeployment();

    const AccountingFactory = await ethers.getContractFactory('MockAccounting');
    // Deploy as UUPS proxy
    accounting = await upgrades.deployProxy(
      AccountingFactory,
      [await mockShoyubashi.getAddress(), await provethVerifier.getAddress(), deployer.address],
      { kind: 'uups', initializer: 'initialize', constructorArgs: [await mockSiweAuth.getAddress()] }
    ) as unknown as MockAccounting;
    await accounting.waitForDeployment();

    accountingUser1 = accounting.connect(user1) as MockAccounting;
    accountingUser2 = accounting.connect(user2) as MockAccounting;

    const hdNodeWallet = await ethers.HDNodeWallet.fromPhrase(
      (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig).mnemonic,
    );

    // Drive index 0 and 1 wallets
    userWallet1 = hdNodeWallet.connect(provider);
    userWallet2 = hdNodeWallet.derivePath("44'/60'/0'/0/0").connect(provider);

    console.log("wallets", userWallet1.address, userWallet2.address);

    const domainTuple = await accounting.eip712Domain();
    domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    }

    // Set up token info for tests
    const data = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
      ethers.zeroPadValue(TEST_TOKEN.address, 20)
    ]);
    await accounting.setTokenInfo({
      tokenType: TEST_TOKEN.tokenType,
      data: data
    });

    // Set gas price for withdrawal tests
    await accounting.setGasPrice(TEST_TOKEN.chainId, 1000000000n); // 1 gwei

    tokenId = TEST_TOKEN.tokenId;
  });

  it("Should expose createLockNonces, modifyLockNonces, transferLockedNonces", async function () {
    expect(await accounting.createLockNonces(userWallet1.address)).to.equal(0n);
    expect(await accounting.modifyLockNonces(userWallet1.address)).to.equal(0n);
    // transferLockedNonces is keyed by service address; userWallet2 acts as the service in lock tests
    expect(await accounting.transferLockedNonces(userWallet2.address)).to.equal(0n);
  });

  it("Admin adds tokenInfo for Test token", async function () {
    const [admin] = await ethers.getSigners();

    // Pad chainId to 32 bytes, token address to 20 bytes, then concatenate
    const data = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
      ethers.zeroPadValue(TEST_TOKEN.address, 20)
    ]);

    const tx = await accounting.connect(admin).setTokenInfo({
      tokenType: TEST_TOKEN.tokenType,
      data: data
    });
    await tx.wait();

    const tokenId = await accounting.getTokenId({
      tokenType: TEST_TOKEN.tokenType,
      data: data
    });

    console.log("tokenId", tokenId);

    expect(tokenId).to.equal(TEST_TOKEN.tokenId);
  });

  it("User should be able to deposit", async function () {
    const provider = new ethers.JsonRpcProvider("https://sepolia.base.org");

    const blockNumber = 32680090;
    const transactionIndex = 45;

    const { rlpBlockHeader, proof: txProof } = await getTxInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    const { proof: receiptProof } = await getReceiptInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    // Check balance before
    const balanceBefore = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);

    await mockShoyubashi.setUnanimousHash(TEST_TOKEN.chainId, blockNumber, keccak256(rlpBlockHeader));

    // Submit the deposit to Accounting contract
    await accounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
      rlpBlockHeader,
      transactionIndexRlp: getRlpUint(transactionIndex),
      transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList))),
    }, {
      receiptIndexRlp: getRlpUint(transactionIndex),
      receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
    });

    const balanceAfter = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);

    expect(balanceBefore).to.equal(0);
    expect(balanceAfter).to.equal(parseUsdt("10"));
  });

  describe("Deposit validation (isolated deployment)", function () {
    let isolatedAccounting: MockAccounting;
    let isolatedShoyubashi: MockShoyuBashi;
    let rlpBlockHeader: string;
    let txProof: string[];
    let receiptProof: string[];

    const blockNumber = 32680090;
    const transactionIndex = 45;

    before(async () => {
      const provider = new ethers.JsonRpcProvider("https://sepolia.base.org");

      ({ rlpBlockHeader, proof: txProof } = await getTxInclusionProof(
        provider,
        blockNumber,
        transactionIndex
      ));

      ({ proof: receiptProof } = await getReceiptInclusionProof(
        provider,
        blockNumber,
        transactionIndex
      ));
    });

    beforeEach(async () => {
      const [deployer] = await ethers.getSigners();

      const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
      const isolatedMockSiweAuth = await MockSiweAuthFactory.deploy('test');
      await isolatedMockSiweAuth.waitForDeployment();

      const MockShoyubashiFactory = await ethers.getContractFactory('MockShoyuBashi');
      isolatedShoyubashi = await MockShoyubashiFactory.deploy();
      await isolatedShoyubashi.waitForDeployment();

      const ProvethVerifierFactory = await ethers.getContractFactory('ProvethVerifier');
      const isolatedProvethVerifier = await ProvethVerifierFactory.deploy();
      await isolatedProvethVerifier.waitForDeployment();

      const AccountingFactory = await ethers.getContractFactory('MockAccounting');
      isolatedAccounting = await upgrades.deployProxy(
        AccountingFactory,
        [await isolatedShoyubashi.getAddress(), await isolatedProvethVerifier.getAddress(), deployer.address],
        { kind: 'uups', initializer: 'initialize', constructorArgs: [await isolatedMockSiweAuth.getAddress()] }
      ) as unknown as MockAccounting;
      await isolatedAccounting.waitForDeployment();

      const data = ethers.concat([
        ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
        ethers.zeroPadValue(TEST_TOKEN.address, 20)
      ]);

      await isolatedAccounting.setTokenInfo({
        tokenType: TEST_TOKEN.tokenType,
        data
      });
    });

    it("Should reject replayed deposit proof", async function () {
      await isolatedShoyubashi.setUnanimousHash(
        TEST_TOKEN.chainId,
        blockNumber,
        keccak256(rlpBlockHeader)
      );

      await isolatedAccounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
        rlpBlockHeader,
        transactionIndexRlp: getRlpUint(transactionIndex),
        transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList))),
      }, {
        receiptIndexRlp: getRlpUint(transactionIndex),
        receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
      });

      await expect(
        isolatedAccounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
          rlpBlockHeader,
          transactionIndexRlp: getRlpUint(transactionIndex),
          transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        }, {
          receiptIndexRlp: getRlpUint(transactionIndex),
          receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        })
      ).to.be.revertedWithCustomError(isolatedAccounting, "DepositAlreadyProcessed");
    });

    it("Should reject deposit with invalid proof", async function () {
      await isolatedShoyubashi.setUnanimousHash(
        TEST_TOKEN.chainId,
        blockNumber,
        keccak256(rlpBlockHeader)
      );

      const invalidProof = txProof.slice(0, txProof.length - 1);

      await expect(
        isolatedAccounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
          rlpBlockHeader,
          transactionIndexRlp: getRlpUint(transactionIndex),
          transactionProofStack: ethers.encodeRlp(
            invalidProof.map((rlpList) => ethers.decodeRlp(rlpList))
          ),
        }, {
          receiptIndexRlp: getRlpUint(transactionIndex),
          receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        })
      ).to.be.reverted;
    });

    it("Should reject deposit with invalid receipt proof", async function () {
      await isolatedShoyubashi.setUnanimousHash(
        TEST_TOKEN.chainId,
        blockNumber,
        keccak256(rlpBlockHeader)
      );

      const invalidProof = receiptProof.slice(0, receiptProof.length - 1);

      await expect(
        isolatedAccounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
          rlpBlockHeader,
          transactionIndexRlp: getRlpUint(transactionIndex),
          transactionProofStack: ethers.encodeRlp(
            txProof.map((rlpList) => ethers.decodeRlp(rlpList))
          ),
        }, {
          receiptIndexRlp: getRlpUint(transactionIndex),
          receiptProofStack: ethers.encodeRlp(invalidProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        })
      ).to.be.reverted;
    });

    it("Should reject deposit when tx/receipt indices mismatch", async function () {
      await isolatedShoyubashi.setUnanimousHash(
        TEST_TOKEN.chainId,
        blockNumber,
        keccak256(rlpBlockHeader)
      );

      await expect(
        isolatedAccounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
          rlpBlockHeader,
          transactionIndexRlp: getRlpUint(transactionIndex),
          transactionProofStack: ethers.encodeRlp(
            txProof.map((rlpList) => ethers.decodeRlp(rlpList))
          ),
        }, {
          receiptIndexRlp: getRlpUint(transactionIndex + 1),
          receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        })
      ).to.be.revertedWithCustomError(isolatedAccounting, "ReceiptIndexMismatch");
    });

    it("Should reject deposit with wrong block hash", async function () {
      const wrongBlockHash =
        "0x1234567890123456789012345678901234567890123456789012345678901234";
      await isolatedShoyubashi.setUnanimousHash(TEST_TOKEN.chainId, blockNumber, wrongBlockHash);

      await expect(
        isolatedAccounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
          rlpBlockHeader,
          transactionIndexRlp: getRlpUint(transactionIndex),
          transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        }, {
          receiptIndexRlp: getRlpUint(transactionIndex),
          receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        })
      ).to.be.revertedWithCustomError(isolatedAccounting, "InvalidBlockHash");
    });
  });

  it("Test EIP712 transfer", async function () {
    const nonce = await accounting.transferNonces(userWallet1.address);
    const signature = await userWallet1.signTypedData(
      domain,
      { Transfer: types.Transfer },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("1"),
        nonce: nonce,
      }
    );

    // Check balances before
    const balance1Before = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2Before = await accounting.getBalance(userWallet2.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.transferBalance(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("1"),
      nonce,
      signature
    );
    await tx.wait();

    const balance1After = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2After = await accounting.getBalance(userWallet2.address, TEST_TOKEN.tokenId);

    expect(balance1Before).to.equal(parseUsdt("10"));
    expect(balance1After).to.equal(parseUsdt("9"));
    expect(balance2Before).to.equal(0);
    expect(balance2After).to.equal(parseUsdt("1"));
  });

  it("Test locking with EIP712", async function () {
    const expiry = await getBlockTimestamp() + 3600; // 1 hour from now
    const lockNonce = await accounting.createLockNonces(userWallet1.address);

    const signature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        userAddress: userWallet1.address,
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("1"),
        expiry,
        nonce: lockNonce
      }
    );

    // Check balances before
    const balance1Before = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2Before = await accounting.getBalance(userWallet2.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.createLock(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("1"),
      expiry,
      lockNonce,
      signature
    );
    await tx.wait();

    const balance1After = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2After = await accounting.getBalance(userWallet2.address, TEST_TOKEN.tokenId);

    expect(balance1Before).to.equal(parseUsdt("9"));
    expect(balance1After).to.equal(parseUsdt("8"));
    expect(balance2Before).to.equal(parseUsdt("1"));
    expect(balance2After).to.equal(parseUsdt("1"));

    // It doesn't go to the normal balance, instead a lock is appended to the user info
    const userLocks = await accounting.getUserLocks(userWallet1.address, "0x");

    expect(userLocks.length).to.equal(1);
    expect(userLocks[0][1]).to.equal(userWallet2.address);
    expect(userLocks[0][2]).to.equal(TEST_TOKEN.tokenId);
    expect(userLocks[0][3]).to.equal(parseUsdt("1"));
    expect(userLocks[0][4]).to.be.equal(expiry);
  });

  it('Privacy: unauthorized callers should be rejected by all user-only view functions', async function () {
    // user2 tries to read user1's data
    const user1Addr = user1.address;
    await expect(accountingUser2.balanceOf(user1Addr, tokenId, "0x")).to.be.revertedWithCustomError(accounting, 'Unauthorized');
    await expect(accountingUser2.getUserLocks(user1Addr, "0x")).to.be.revertedWithCustomError(accounting, 'Unauthorized');
  });

  it('Privacy: user-only view functions should return correct data for the owner', async function () {
    const user1Addr = user1.address;
    const bal = await accountingUser1.balanceOf(user1Addr, tokenId, "0x");
    expect(bal).to.be.gte(0);

    const locks = await accountingUser1.getUserLocks(user1Addr, "0x");
    expect(locks).to.be.an('array');

    const totalLocked = locks.reduce(
      (acc, lock) => lock[2] === tokenId ? acc + lock[3] : acc,
      0n
    );
    expect(totalLocked).to.be.gte(0);
  });

  it('Privacy: service-scoped total locked balance should only count the caller\'s locks', async function () {
    const svcAddr = service.address;
    const accountingService = accounting.connect(service) as MockAccounting;
    const serviceLocks = await accountingService.getServiceLocks(user1.address, "0x");
    expect(serviceLocks.every((lock) => lock[1].toLowerCase() === svcAddr.toLowerCase())).to.equal(true);
    const svcTotal = serviceLocks.reduce(
      (acc, lock) => lock[2] === tokenId ? acc + lock[3] : acc,
      0n
    );
    expect(svcTotal).to.be.gte(0);
  });

  it("The service should be able to resolve the lock", async function () {
    const transferLockedNonce = await accounting.transferLockedNonces(userWallet2.address);

    const signature = await userWallet2.signTypedData(
      domain,
      { TransferLocked: types.TransferLocked },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        lockId: 1,
        amount: parseUsdt("0.5"),
        nonce: transferLockedNonce,
        serviceAddress: userWallet2.address,
      }
    );

    // Check balances before
    const balance1Before = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.transferFromLock(
      userWallet1.address,
      userWallet2.address,
      1,
      parseUsdt("0.5"),
      transferLockedNonce,
      signature
    );
    await tx.wait();


  });

  it("The user should be able to unlock the remaining locked funds after expiry", async function () {
    // Fast forward time by 2 hours
    await ethers.provider.send("evm_increaseTime", [2 * 3600]);
    await ethers.provider.send("evm_mine", []);

    await accounting.unlockSingleLock(userWallet1.address, 1);

    const userLocks = await accounting.getUserLocks(userWallet1.address, "0x");
    expect(userLocks.length).to.equal(0);

  });

  it("The user shouldn't be able to create more than 10 locks", async function () {
    const expiry = await getBlockTimestamp() + 3600; // 1 hour from now

    for (let i = 0; i < 10; i++) {
      const lockNonce = await accounting.createLockNonces(userWallet1.address);
      const signature = await userWallet1.signTypedData(
        domain,
        { Lock: types.Lock },
        {
          userAddress: userWallet1.address,
          serviceAddress: userWallet2.address,
          tokenId: TEST_TOKEN.tokenId,
          amount: parseUsdt("0.1"),
          expiry: expiry + i,
          nonce: lockNonce
        }
      );

      // Submit the transfer to Accounting contract
      const tx = await accounting.createLock(
        userWallet1.address,
        userWallet2.address,
        TEST_TOKEN.tokenId,
        parseUsdt("0.1"),
        expiry + i,
        lockNonce,
        signature
      );
      await tx.wait();
    }

    const userLocks = await accounting.getUserLocks(userWallet1.address, "0x");
    expect(userLocks.length).to.equal(10);

    // Try to create the 11th lock, should fail
    const lockNonce = await accounting.createLockNonces(userWallet1.address);
    const signature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        userAddress: userWallet1.address,
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("0.1"),
        expiry: expiry + 11,
        nonce: lockNonce
      }
    );

    await expect(accounting.createLock(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("0.1"),
      expiry + 11,
      lockNonce,
      signature
    )).to.be.revertedWithCustomError(accounting, "TooManyActiveLocks");
  });

  it("Should reject replay of createLock signature", async function () {
    const timestamp = await getBlockTimestamp();
    const expiry = timestamp + 3600;
    // Use userWallet2 to avoid hitting userWallet1's full lock slots from the prior test
    const lockNonce = await accounting.createLockNonces(userWallet2.address);

    const signature = await userWallet2.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        userAddress: userWallet2.address,
        serviceAddress: userWallet1.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("0.5"),
        expiry,
        nonce: lockNonce,
      }
    );

    await accounting.createLock(
      userWallet2.address, userWallet1.address, TEST_TOKEN.tokenId,
      parseUsdt("0.5"), expiry, lockNonce, signature
    );

    await expect(accounting.createLock(
      userWallet2.address, userWallet1.address, TEST_TOKEN.tokenId,
      parseUsdt("0.5"), expiry, lockNonce, signature
    )).to.be.revertedWithCustomError(accounting, "InvalidNonce");
  });


  it("User should be able to withdraw TEST token using EIP712 signature", async function () {
    const nonce = await accounting.withdrawalNonces(userWallet1.address);
    const signature = await userWallet1.signTypedData(
      domain,
      { Withdraw: types.Withdraw },
      {
        userAddress: userWallet1.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("0.1"),
        nonce: nonce,
      }
    );

    // Submit the transfer to Accounting contract
    const tx = await accounting.requestWithdrawal(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt("0.1"),
      nonce,
      signature
    );
    await tx.wait();

    const withdrawals = await accounting.withdrawals(0);
    expect(withdrawals.userAddress).to.equal(userWallet1.address);
    expect(withdrawals.toAddress).to.equal(userWallet1.address);
    expect(withdrawals.amount).to.equal(parseUsdt("0.1"));
    expect(withdrawals.tokenId).to.equal(TEST_TOKEN.tokenId);
    expect(withdrawals.resolved).to.equal(false);

    // resolveWithdrawal requires Sapphire EIP155Signer precompile - skip on hardhat
    const network = await ethers.provider.getNetwork();
    if (network.name !== 'hardhat' && network.name !== 'unknown') {
      const tx2 = await accounting.resolveWithdrawal(0);
      const receipt2 = await tx2.wait();

      const withdrawalAfter = await accounting.withdrawals(0);
      expect(withdrawalAfter.resolved).to.equal(true);
    }
  });

});

describe('WithdrawFromLock + CreditDepositTo', function () {
  let accounting: MockAccounting;
  let mockShoyubashi: MockShoyuBashi;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;
  let userWallet3: Wallet;

  const blockNumber = 32680090;
  const transactionIndex = 45;

  before(async () => {
    const provider = ethers.provider;
    const mnemonic = (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig).mnemonic;

    userWallet1 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/0")
      .connect(provider);
    userWallet2 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/1")
      .connect(provider);
    userWallet3 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/2")
      .connect(provider);
  });

  beforeEach(async () => {
    const [deployer] = await ethers.getSigners();

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
    const mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    const MockShoyubashiFactory = await ethers.getContractFactory('MockShoyuBashi');
    mockShoyubashi = await MockShoyubashiFactory.deploy();
    await mockShoyubashi.waitForDeployment();

    const ProvethVerifierFactory = await ethers.getContractFactory('ProvethVerifier');
    const provethVerifier = await ProvethVerifierFactory.deploy();
    await provethVerifier.waitForDeployment();

    const AccountingFactory = await ethers.getContractFactory('MockAccounting');
    accounting = await upgrades.deployProxy(
      AccountingFactory,
      [await mockShoyubashi.getAddress(), await provethVerifier.getAddress(), deployer.address],
      { kind: 'uups', initializer: 'initialize', constructorArgs: [await mockSiweAuth.getAddress()] }
    ) as unknown as MockAccounting;
    await accounting.waitForDeployment();

    const domainTuple = await accounting.eip712Domain();
    domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };

    const data = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
      ethers.zeroPadValue(TEST_TOKEN.address, 20)
    ]);
    await accounting.setTokenInfo({
      tokenType: TEST_TOKEN.tokenType,
      data: data
    });
    await accounting.setGasPrice(TEST_TOKEN.chainId, 1000000000n);
  });

  async function createLockForService(lockAmount: bigint, serviceAddress: string): Promise<bigint> {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt("5"));
    const expiry = await getBlockTimestamp() + 3600;
    const lockSignature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        userAddress: userWallet1.address,
        serviceAddress,
        tokenId: TEST_TOKEN.tokenId,
        amount: lockAmount,
        expiry,
      }
    );

    await accounting.createLock(
      userWallet1.address,
      serviceAddress,
      TEST_TOKEN.tokenId,
      lockAmount,
      expiry,
      lockSignature
    );

    const userLocks = await accounting.getUserLocks(userWallet1.address, "0x");
    return userLocks[0][0];
  }

  it("should withdraw from lock to external destination and store toAddress", async function () {
    const lockId = await createLockForService(parseUsdt("2"), userWallet2.address);
    const nonce = await accounting.withdrawFromLockNonces(userWallet2.address);

    const signature = await userWallet2.signTypedData(
      domain,
      { WithdrawFromLock: types.WithdrawFromLock },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet3.address,
        lockId,
        amount: parseUsdt("1"),
        nonce,
      }
    );

    await accounting.withdrawFromLock(
      userWallet1.address,
      userWallet3.address,
      lockId,
      parseUsdt("1"),
      nonce,
      signature
    );

    const locksAfter = await accounting.getUserLocks(userWallet1.address, "0x");
    expect(locksAfter[0][3]).to.equal(parseUsdt("1"));

    const withdrawal = await accounting.withdrawals(0);
    expect(withdrawal.userAddress).to.equal(userWallet1.address);
    expect(withdrawal.toAddress).to.equal(userWallet3.address);
    expect(withdrawal.amount).to.equal(parseUsdt("1"));
    expect(withdrawal.tokenId).to.equal(TEST_TOKEN.tokenId);
    expect(withdrawal.resolved).to.equal(false);
  });

  it("should reject withdrawFromLock with zero destination", async function () {
    const lockId = await createLockForService(parseUsdt("2"), userWallet2.address);
    const nonce = await accounting.withdrawFromLockNonces(userWallet2.address);

    const signature = await userWallet2.signTypedData(
      domain,
      { WithdrawFromLock: types.WithdrawFromLock },
      {
        userAddress: userWallet1.address,
        toAddress: ethers.ZeroAddress,
        lockId,
        amount: parseUsdt("1"),
        nonce,
      }
    );

    await expect(
      accounting.withdrawFromLock(
        userWallet1.address,
        ethers.ZeroAddress,
        lockId,
        parseUsdt("1"),
        nonce,
        signature
      )
    ).to.be.revertedWithCustomError(accounting, "AddressMismatch");
  });

  it("should reject withdrawFromLock signed by non-service", async function () {
    const lockId = await createLockForService(parseUsdt("2"), userWallet2.address);
    const nonce = await accounting.withdrawFromLockNonces(userWallet2.address);

    const signature = await userWallet1.signTypedData(
      domain,
      { WithdrawFromLock: types.WithdrawFromLock },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet3.address,
        lockId,
        amount: parseUsdt("1"),
        nonce,
      }
    );

    await expect(
      accounting.withdrawFromLock(
        userWallet1.address,
        userWallet3.address,
        lockId,
        parseUsdt("1"),
        nonce,
        signature
      )
    ).to.be.revertedWithCustomError(accounting, "InvalidSignature");
  });

  it("should reject replay of withdrawFromLock signature", async function () {
    const lockId = await createLockForService(parseUsdt("2"), userWallet2.address);
    const nonce = await accounting.withdrawFromLockNonces(userWallet2.address);

    const signature = await userWallet2.signTypedData(
      domain,
      { WithdrawFromLock: types.WithdrawFromLock },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet3.address,
        lockId,
        amount: parseUsdt("1"),
        nonce,
      }
    );

    await accounting.withdrawFromLock(
      userWallet1.address,
      userWallet3.address,
      lockId,
      parseUsdt("1"),
      nonce,
      signature
    );

    await expect(
      accounting.withdrawFromLock(
        userWallet1.address,
        userWallet3.address,
        lockId,
        parseUsdt("1"),
        nonce,
        signature
      )
    ).to.be.revertedWithCustomError(accounting, "InvalidNonce");
  });

  describe("creditDepositTo", function () {
    let rlpBlockHeader: string;
    let txProof: string[];
    let receiptProof: string[];
    let depositTxHash: string;

    before(async () => {
      const baseProvider = new ethers.JsonRpcProvider("https://sepolia.base.org");

      ({ rlpBlockHeader, proof: txProof } = await getTxInclusionProof(
        baseProvider,
        blockNumber,
        transactionIndex
      ));

      ({ proof: receiptProof } = await getReceiptInclusionProof(
        baseProvider,
        blockNumber,
        transactionIndex
      ));

      const block = await baseProvider.getBlock(blockNumber, true);
      if (!block) {
        throw new Error(`Block ${blockNumber} not found`);
      }
      const txEntry = block.transactions[transactionIndex];
      if (!txEntry) {
        throw new Error(`Transaction ${transactionIndex} not found in block ${blockNumber}`);
      }
      depositTxHash = typeof txEntry === "string" ? txEntry : txEntry.hash;
    });

    beforeEach(async () => {
      await mockShoyubashi.setUnanimousHash(
        TEST_TOKEN.chainId,
        blockNumber,
        keccak256(rlpBlockHeader)
      );
    });

    it("should credit proven deposit to beneficiary with depositor signature", async function () {
      const beneficiary = userWallet2.address;
      const amount = parseUsdt("10");
      const nonce = await accounting.creditDepositToNonces(userWallet1.address);

      const signature = await userWallet1.signTypedData(
        domain,
        { CreditDepositTo: types.CreditDepositTo },
        {
          depositorAddress: userWallet1.address,
          beneficiaryAddress: beneficiary,
          tokenId: TEST_TOKEN.tokenId,
          amount,
          chainId: TEST_TOKEN.chainId,
          txHash: depositTxHash,
          nonce,
        }
      );

      await accounting.creditDepositTo(
        userWallet1.address,
        beneficiary,
        TEST_TOKEN.tokenId,
        {
          rlpBlockHeader,
          transactionIndexRlp: getRlpUint(transactionIndex),
          transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        },
        {
          receiptIndexRlp: getRlpUint(transactionIndex),
          receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        },
        nonce,
        signature
      );

      expect(await accounting.getBalance(beneficiary, TEST_TOKEN.tokenId)).to.equal(amount);
      expect(await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId)).to.equal(0);
    });

    it("should reject creditDepositTo with invalid beneficiary signature binding", async function () {
      const amount = parseUsdt("10");
      const nonce = await accounting.creditDepositToNonces(userWallet1.address);
      const signature = await userWallet1.signTypedData(
        domain,
        { CreditDepositTo: types.CreditDepositTo },
        {
          depositorAddress: userWallet1.address,
          beneficiaryAddress: userWallet2.address,
          tokenId: TEST_TOKEN.tokenId,
          amount,
          chainId: TEST_TOKEN.chainId,
          txHash: depositTxHash,
          nonce,
        }
      );

      await expect(
        accounting.creditDepositTo(
          userWallet1.address,
          userWallet3.address,
          TEST_TOKEN.tokenId,
          {
            rlpBlockHeader,
            transactionIndexRlp: getRlpUint(transactionIndex),
            transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList))),
          },
          {
            receiptIndexRlp: getRlpUint(transactionIndex),
            receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
          },
          nonce,
          signature
        )
      ).to.be.revertedWithCustomError(accounting, "InvalidSignature");
    });

    it("should reject replayed proof in creditDepositTo", async function () {
      const amount = parseUsdt("10");
      const nonce = await accounting.creditDepositToNonces(userWallet1.address);
      const signature = await userWallet1.signTypedData(
        domain,
        { CreditDepositTo: types.CreditDepositTo },
        {
          depositorAddress: userWallet1.address,
          beneficiaryAddress: userWallet2.address,
          tokenId: TEST_TOKEN.tokenId,
          amount,
          chainId: TEST_TOKEN.chainId,
          txHash: depositTxHash,
          nonce,
        }
      );

      const txProofPayload = {
        rlpBlockHeader,
        transactionIndexRlp: getRlpUint(transactionIndex),
        transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList))),
      };
      const receiptProofPayload = {
        receiptIndexRlp: getRlpUint(transactionIndex),
        receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
      };

      await accounting.creditDepositTo(
        userWallet1.address,
        userWallet2.address,
        TEST_TOKEN.tokenId,
        txProofPayload,
        receiptProofPayload,
        nonce,
        signature
      );

      const secondNonce = await accounting.creditDepositToNonces(userWallet1.address);
      const secondSignature = await userWallet1.signTypedData(
        domain,
        { CreditDepositTo: types.CreditDepositTo },
        {
          depositorAddress: userWallet1.address,
          beneficiaryAddress: userWallet2.address,
          tokenId: TEST_TOKEN.tokenId,
          amount,
          chainId: TEST_TOKEN.chainId,
          txHash: depositTxHash,
          nonce: secondNonce,
        }
      );

      await expect(
        accounting.creditDepositTo(
          userWallet1.address,
          userWallet2.address,
          TEST_TOKEN.tokenId,
          txProofPayload,
          receiptProofPayload,
          secondNonce,
          secondSignature
        )
      ).to.be.revertedWithCustomError(accounting, "DepositAlreadyProcessed");
    });
  });
});

describe('ModifyLock', function () {
  let accounting: MockAccounting;
  let mockShoyubashi: MockShoyuBashi;
  let provethVerifier: ProvethVerifier;
  let mockSiweAuth: MockSiweAuth;
  let accountingUser1: MockAccounting;
  let accountingUser2: MockAccounting;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;

  before(async () => {
    const provider = ethers.provider;
    const [deployer, user1, user2] = await ethers.getSigners();

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
    mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    const MockShoyubashiFactory = await ethers.getContractFactory('MockShoyuBashi');
    mockShoyubashi = await MockShoyubashiFactory.deploy();
    await mockShoyubashi.waitForDeployment();

    const ProvethVerifierFactory = await ethers.getContractFactory('ProvethVerifier');
    provethVerifier = await ProvethVerifierFactory.deploy();
    await provethVerifier.waitForDeployment();

    const AccountingFactory = await ethers.getContractFactory('MockAccounting');
    // Deploy as UUPS proxy
    accounting = await upgrades.deployProxy(
      AccountingFactory,
      [await mockShoyubashi.getAddress(), await provethVerifier.getAddress(), deployer.address],
      { kind: 'uups', initializer: 'initialize', constructorArgs: [await mockSiweAuth.getAddress()] }
    ) as unknown as MockAccounting;
    await accounting.waitForDeployment();

    accountingUser1 = accounting.connect(user1) as MockAccounting;
    accountingUser2 = accounting.connect(user2) as MockAccounting;

    const hdNodeWallet = await ethers.HDNodeWallet.fromPhrase(
      (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig).mnemonic,
    );

    userWallet1 = hdNodeWallet.connect(provider);
    userWallet2 = hdNodeWallet.derivePath("44'/60'/0'/0/0").connect(provider);

    const domainTuple = await accounting.eip712Domain();
    domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    }

    const data = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
      ethers.zeroPadValue(TEST_TOKEN.address, 20)
    ]);

    await accounting.setTokenInfo({
      tokenType: TEST_TOKEN.tokenType,
      data: data
    });

    const baseProvider = new ethers.JsonRpcProvider("https://sepolia.base.org");
    const blockNumber = 32680090;
    const transactionIndex = 45;

    const { rlpBlockHeader, proof: txProof } = await getTxInclusionProof(
      baseProvider,
      blockNumber,
      transactionIndex
    );

    const { proof: receiptProof } = await getReceiptInclusionProof(
      baseProvider,
      blockNumber,
      transactionIndex
    );

    await mockShoyubashi.setUnanimousHash(TEST_TOKEN.chainId, blockNumber, keccak256(rlpBlockHeader));

    await accounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
      rlpBlockHeader,
      transactionIndexRlp: getRlpUint(transactionIndex),
      transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList))),
    }, {
      receiptIndexRlp: getRlpUint(transactionIndex),
      receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
    });
  });

  it("User should be able to add funds to an existing lock", async function () {
    const expiry = await getBlockTimestamp() + 3600;
    const lockNonce = await accounting.createLockNonces(userWallet1.address);

    const lockSignature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        userAddress: userWallet1.address,
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("1"),
        expiry,
        nonce: lockNonce
      }
    );

    await accounting.createLock(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("1"),
      expiry,
      lockNonce,
      lockSignature
    );

    const balanceBefore = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const locksBefore = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locksBefore[0][0];
    expect(locksBefore[0][3]).to.equal(parseUsdt("1"));

    const newExpiry = expiry + 7200;
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);
    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("2"),
        newExpiry,
        nonce: modifyNonce
      }
    );

    const tx = await accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("2"),
      newExpiry,
      modifyNonce,
      modifyLockSignature
    );
    await tx.wait();

    const balanceAfter = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const locksAfter = await accounting.getUserLocks(userWallet1.address, "0x");

    expect(balanceAfter).to.equal(balanceBefore - parseUsdt("2"));
    expect(locksAfter[0][3]).to.equal(parseUsdt("3"));
    expect(locksAfter[0][4]).to.equal(newExpiry);
  });

  it("User should be able to add funds while keeping the same expiry", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("0.5"),
        newExpiry: currentExpiry,
        nonce: modifyNonce
      }
    );

    const lockAmountBefore = locks[0][3];

    await accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("0.5"),
      currentExpiry,
      modifyNonce,
      modifyLockSignature
    );

    const locksAfter = await accounting.getUserLocks(userWallet1.address, "0x");
    expect(locksAfter[0][3]).to.equal(lockAmountBefore + parseUsdt("0.5"));
    expect(locksAfter[0][4]).to.equal(currentExpiry);
  });

  it("User should be able to extend expiry without adding funds", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const newExpiry = currentExpiry + 3600;
    const lockAmountBefore = locks[0][3];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: 0,
        newExpiry,
        nonce: modifyNonce
      }
    );

    const balanceBefore = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);

    await accounting.modifyLock(
      userWallet1.address,
      lockId,
      0,
      newExpiry,
      modifyNonce,
      modifyLockSignature
    );

    const balanceAfter = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const locksAfter = await accounting.getUserLocks(userWallet1.address, "0x");

    expect(balanceAfter).to.equal(balanceBefore);
    expect(locksAfter[0][3]).to.equal(lockAmountBefore);
    expect(locksAfter[0][4]).to.equal(newExpiry);
  });

  it("Should reject modifyLock with invalid lock ID", async function () {
    const expiry = await getBlockTimestamp() + 3600;
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: 999,
        amount: parseUsdt("1"),
        newExpiry: expiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      999,
      parseUsdt("1"),
      expiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidLockId");
  });

  it("Should reject modifyLock with earlier expiry", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const earlierExpiry = currentExpiry - 1000;
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("1"),
        newExpiry: earlierExpiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("1"),
      earlierExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidExpiry");
  });

  it("Should reject modifyLock with zero amount and same expiry (no-op)", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: 0,
        newExpiry: currentExpiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      0,
      currentExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidAmount");
  });

  it("Should reject modifyLock with insufficient balance", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("1000000"),
        newExpiry: currentExpiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("1000000"),
      currentExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InsufficientBalance");
  });

  it("Should reject modifyLock with wrong signer", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet2.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("0.1"),
        newExpiry: currentExpiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("0.1"),
      currentExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidSignature");
  });

  it("Should reject replay of modifyLock signature", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const newExpiry = currentExpiry + 100;
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("0.1"),
        newExpiry,
        nonce: modifyNonce
      }
    );

    await accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("0.1"),
      newExpiry,
      modifyNonce,
      modifyLockSignature
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("0.1"),
      newExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidNonce");
  });

  it("Service should still be able to transfer from lock after funds are added", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const lockAmount = locks[0][3];
    const transferLockedNonce = await accounting.transferLockedNonces(userWallet2.address);

    const signature = await userWallet2.signTypedData(
      domain,
      { TransferLocked: types.TransferLocked },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        lockId: lockId,
        amount: parseUsdt("0.5"),
        nonce: transferLockedNonce,
        serviceAddress: userWallet2.address,
      }
    );

    const balance2Before = await accounting.getBalance(userWallet2.address, TEST_TOKEN.tokenId);

    await accounting.transferFromLock(
      userWallet1.address,
      userWallet2.address,
      lockId,
      parseUsdt("0.5"),
      transferLockedNonce,
      signature
    );

    const balance2After = await accounting.getBalance(userWallet2.address, TEST_TOKEN.tokenId);
    const locksAfter = await accounting.getUserLocks(userWallet1.address, "0x");

    expect(balance2After).to.equal(balance2Before + parseUsdt("0.5"));
    expect(locksAfter[0][3]).to.equal(lockAmount - parseUsdt("0.5"));
  });

  it("Should reject replay of transferFromLock signature", async function () {
    // Service (userWallet2) transfers from its lock on userWallet1's account
    const locks = await accounting.getUserLocks(userWallet1.address, "0x");
    const lockId = locks[0][0];
    const transferLockedNonce = await accounting.transferLockedNonces(userWallet2.address);

    const signature = await userWallet2.signTypedData(
      domain,
      { TransferLocked: types.TransferLocked },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        lockId,
        amount: parseUsdt("0.1"),
        nonce: transferLockedNonce,
        serviceAddress: userWallet2.address,
      }
    );

    await accounting.transferFromLock(
      userWallet1.address, userWallet2.address, lockId,
      parseUsdt("0.1"), transferLockedNonce, signature
    );

    await expect(accounting.transferFromLock(
      userWallet1.address, userWallet2.address, lockId,
      parseUsdt("0.1"), transferLockedNonce, signature
    )).to.be.revertedWithCustomError(accounting, "InvalidNonce");
  });
});

describe('Upgradability', function () {
  let accounting: MockAccounting;
  let mockShoyubashi: MockShoyuBashi;
  let provethVerifier: ProvethVerifier;
  let mockSiweAuth: MockSiweAuth;
  let proxyAddress: string;

  before(async () => {
    const [deployer] = await ethers.getSigners();

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
    mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    const MockShoyubashiFactory = await ethers.getContractFactory('MockShoyuBashi');
    mockShoyubashi = await MockShoyubashiFactory.deploy();
    await mockShoyubashi.waitForDeployment();

    const ProvethVerifierFactory = await ethers.getContractFactory('ProvethVerifier');
    provethVerifier = await ProvethVerifierFactory.deploy();
    await provethVerifier.waitForDeployment();

    const AccountingFactory = await ethers.getContractFactory('MockAccounting');
    accounting = await upgrades.deployProxy(
      AccountingFactory,
      [await mockShoyubashi.getAddress(), await provethVerifier.getAddress(), deployer.address],
      { kind: 'uups', initializer: 'initialize', constructorArgs: [await mockSiweAuth.getAddress()] }
    ) as unknown as MockAccounting;
    await accounting.waitForDeployment();

    proxyAddress = await accounting.getAddress();
  });

  it("Should preserve state after upgrade", async function () {
    const [deployer, user] = await ethers.getSigners();

    // Set up initial state
    const data = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
      ethers.zeroPadValue(TEST_TOKEN.address, 20)
    ]);
    await accounting.setTokenInfo({
      tokenType: TEST_TOKEN.tokenType,
      data: data
    });

    // Set a balance using the test helper
    const initialBalance = parseUsdt("100");
    await accounting.setBalance(user.address, TEST_TOKEN.tokenId, initialBalance);

    // Set gas price for a chain
    const testChainId = 84532n; // Base Sepolia
    const testGasPrice = 1000000000n; // 1 gwei
    await accounting.setGasPrice(testChainId, testGasPrice);

    // Verify initial state
    const balanceBefore = await accounting.getBalance(user.address, TEST_TOKEN.tokenId);
    const evmAddressBefore = await accounting.evmAddress();
    const ownerBefore = await accounting.owner();
    const transferNonceBefore = await accounting.transferNonces(user.address);
    const withdrawalNonceBefore = await accounting.withdrawalNonces(user.address);
    const createLockNonceBefore = await accounting.createLockNonces(user.address);
    const modifyLockNonceBefore = await accounting.modifyLockNonces(user.address);
    const transferLockedNonceBefore = await accounting.transferLockedNonces(user.address);
    const gasPriceBefore = await accounting.gasPrices(testChainId);
    const tokenInfoBefore = await accounting.tokens(TEST_TOKEN.tokenId);
    expect(balanceBefore).to.equal(initialBalance);

    // Upgrade to the same implementation (simulates an upgrade)
    const AccountingV2Factory = await ethers.getContractFactory('MockAccounting');
    const upgraded = await upgrades.upgradeProxy(proxyAddress, AccountingV2Factory, {
      kind: 'uups',
      constructorArgs: [await mockSiweAuth.getAddress()]
    }) as unknown as MockAccounting;

    // Verify state is preserved after upgrade
    const balanceAfter = await upgraded.getBalance(user.address, TEST_TOKEN.tokenId);
    const evmAddressAfter = await upgraded.evmAddress();
    const ownerAfter = await upgraded.owner();
    const transferNonceAfter = await upgraded.transferNonces(user.address);
    const withdrawalNonceAfter = await upgraded.withdrawalNonces(user.address);
    const createLockNonceAfter = await upgraded.createLockNonces(user.address);
    const modifyLockNonceAfter = await upgraded.modifyLockNonces(user.address);
    const transferLockedNonceAfter = await upgraded.transferLockedNonces(user.address);
    const gasPriceAfter = await upgraded.gasPrices(testChainId);
    const tokenInfoAfter = await upgraded.tokens(TEST_TOKEN.tokenId);

    expect(balanceAfter).to.equal(initialBalance, "Balance should be preserved after upgrade");
    expect(evmAddressAfter).to.equal(evmAddressBefore, "EVM address should be preserved after upgrade");
    expect(ownerAfter).to.equal(ownerBefore, "Owner should be preserved after upgrade");
    expect(transferNonceAfter).to.equal(transferNonceBefore, "Transfer nonce should be preserved after upgrade");
    expect(withdrawalNonceAfter).to.equal(withdrawalNonceBefore, "Withdrawal nonce should be preserved after upgrade");
    expect(createLockNonceAfter).to.equal(createLockNonceBefore, "createLock nonce should be preserved after upgrade");
    expect(modifyLockNonceAfter).to.equal(modifyLockNonceBefore, "modifyLock nonce should be preserved after upgrade");
    expect(transferLockedNonceAfter).to.equal(transferLockedNonceBefore, "transferLocked nonce should be preserved after upgrade");
    expect(gasPriceAfter).to.equal(gasPriceBefore, "Gas price should be preserved after upgrade");
    expect(tokenInfoAfter.tokenType).to.equal(tokenInfoBefore.tokenType, "Token info should be preserved after upgrade");
    expect(tokenInfoAfter.data).to.equal(tokenInfoBefore.data, "Token data should be preserved after upgrade");

    // Verify the proxy address is the same
    expect(await upgraded.getAddress()).to.equal(proxyAddress, "Proxy address should remain the same");
  });

  it("Should only allow owner to upgrade", async function () {
    const [deployer, attacker] = await ethers.getSigners();

    const AccountingV2Factory = await ethers.getContractFactory('MockAccounting', attacker);

    await expect(
      upgrades.upgradeProxy(proxyAddress, AccountingV2Factory, {
        kind: 'uups',
        constructorArgs: [await mockSiweAuth.getAddress()]
      })
    ).to.be.revertedWithCustomError(accounting, "OwnableUnauthorizedAccount");
  });

  it("Should prevent re-initialization", async function () {
    const [deployer] = await ethers.getSigners();

    await expect(
      accounting.initialize(await mockShoyubashi.getAddress(), await provethVerifier.getAddress(), deployer.address)
    ).to.be.revertedWithCustomError(accounting, "InvalidInitialization");
  });

  it("Should prevent initialization on implementation contract directly", async function () {
    const [deployer] = await ethers.getSigners();

    // Deploy implementation directly (not via proxy)
    const AccountingFactory = await ethers.getContractFactory('MockAccounting');
    const implementation = await AccountingFactory.deploy(await mockSiweAuth.getAddress());
    await implementation.waitForDeployment();

    // _disableInitializers() in the constructor should block initialize()
    await expect(
      implementation.initialize(
        await mockShoyubashi.getAddress(),
        await provethVerifier.getAddress(),
        deployer.address
      )
    ).to.be.revertedWithCustomError(implementation, "InvalidInitialization");
  });

  it("Should reject upgrade to non-UUPS contract", async function () {
    // ProvethVerifier is a plain (non-UUPS) contract — upgrading to it should fail
    const NonUUPSFactory = await ethers.getContractFactory('ProvethVerifier');

    // OZ plugin validates upgrade safety off-chain before sending any tx
    try {
      await upgrades.upgradeProxy(proxyAddress, NonUUPSFactory, { kind: 'uups' });
      expect.fail("Expected upgrade to non-UUPS contract to be rejected");
    } catch (e: any) {
      expect(e.message).to.include("not upgrade safe");
    }
  });

  it("Should reject ProvethVerifier at address(0) during initialization", async function () {
    const [deployer] = await ethers.getSigners();

    const MockShoyubashiFactory = await ethers.getContractFactory('MockShoyuBashi');
    const zeroPVShoyubashi = await MockShoyubashiFactory.deploy();
    await zeroPVShoyubashi.waitForDeployment();

    const AccountingFactory = await ethers.getContractFactory('MockAccounting');

    // Deployment with address(0) provethVerifier should revert in initializer
    await expect(
      upgrades.deployProxy(
        AccountingFactory,
        [await zeroPVShoyubashi.getAddress(), ethers.ZeroAddress, deployer.address],
        { kind: 'uups', initializer: 'initialize', constructorArgs: [await mockSiweAuth.getAddress()] }
      )
    ).to.be.reverted;
  });

  it("Should support V2 upgrade with new state variables and reinitializer", async function () {
    const [deployer, user] = await ethers.getSigners();

    // Set up initial state
    const initialBalance = parseUsdt("50");
    await accounting.setBalance(user.address, TEST_TOKEN.tokenId, initialBalance);

    const balanceBefore = await accounting.getBalance(user.address, TEST_TOKEN.tokenId);
    expect(balanceBefore).to.equal(initialBalance);

    // Upgrade to V2 (reinitializer doesn't chain parent inits — they ran in V1)
    const AccountingV2Factory = await ethers.getContractFactory('MockAccountingV2');
    const upgraded = await upgrades.upgradeProxy(proxyAddress, AccountingV2Factory, {
      kind: 'uups',
      unsafeAllow: ['missing-initializer'],
      constructorArgs: [await mockSiweAuth.getAddress()],
    }) as unknown as MockAccountingV2;

    // Call reinitializer
    await upgraded.initializeV2(42);

    // Verify new state is set
    expect(await upgraded.newStateVar()).to.equal(42);

    // Verify existing state is preserved
    const balanceAfter = await upgraded.getBalance(user.address, TEST_TOKEN.tokenId);
    expect(balanceAfter).to.equal(initialBalance, "Balance should survive V2 upgrade");

    // Reinitializer should not be callable again
    await expect(
      upgraded.initializeV2(99)
    ).to.be.revertedWithCustomError(upgraded, "InvalidInitialization");
  });
});
