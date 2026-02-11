import { expect, version } from 'chai';
import { ethers, config, upgrades } from 'hardhat';
import { keccak256, parseEther, Wallet } from 'ethers';
import { MockAccounting, MockShoyuBashi, ProvethVerifier } from '../typechain-types';
import { generateERC20Tx, getReceiptInclusionProof, getRlpUint } from './utils';
import { getTxInclusionProof } from './utils';
import { HardhatNetworkHDAccountsConfig } from 'hardhat/types';
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
  ],
  ModifyLock: [
    { name: "userAddress", type: "address" },
    { name: "lockId", type: "uint256" },
    { name: "amount", type: "uint256" },
    { name: "newExpiry", type: "uint256" },
  ],
  TransferLocked: [
    { name: "userAddress", type: "address" },
    { name: "toAddress", type: "address" },
    { name: "lockId", type: "uint256" },
    { name: "amount", type: "uint256" },
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
  ]
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
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;

  before(async () => {
    const provider = ethers.provider;
    const [deployer] = await ethers.getSigners();

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
      { kind: 'uups', initializer: 'initialize' }
    ) as unknown as MockAccounting;
    await accounting.waitForDeployment();

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
    expect(await accounting.decodeEVMErc20TokenData(data)).to.deep.equal([TEST_TOKEN.chainId, TEST_TOKEN.address]);
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
    const balanceBefore = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

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

    const balanceAfter = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

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
        { kind: 'uups', initializer: 'initialize' }
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
    const balance1Before = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2Before = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

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

    const balance1After = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2After = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

    expect(balance1Before).to.equal(parseUsdt("10"));
    expect(balance1After).to.equal(parseUsdt("9"));
    expect(balance2Before).to.equal(0);
    expect(balance2After).to.equal(parseUsdt("1"));
  });

  it("Test locking with EIP712", async function () {
    const expiry = await getBlockTimestamp() + 3600; // 1 hour from now

    const signature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        userAddress: userWallet1.address,
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("1"),
        expiry
      }
    );

    // Check balances before
    const balance1Before = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2Before = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.createLock(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("1"),
      expiry,
      signature
    );
    await tx.wait();

    const balance1After = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2After = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

    expect(balance1Before).to.equal(parseUsdt("9"));
    expect(balance1After).to.equal(parseUsdt("8"));
    expect(balance2Before).to.equal(parseUsdt("1"));
    expect(balance2After).to.equal(parseUsdt("1"));

    // It doesn't go to the normal balance, instead a lock is appended to the user info
    const userLocks = await accounting.getUserLocks(userWallet1.address);

    expect(userLocks.length).to.equal(1);
    expect(userLocks[0][1]).to.equal(userWallet2.address);
    expect(userLocks[0][2]).to.equal(TEST_TOKEN.tokenId);
    expect(userLocks[0][3]).to.equal(parseUsdt("1"));
    expect(userLocks[0][4]).to.be.equal(expiry);
  });

  it("The service should be able to resolve the lock", async function () {
    const signature = await userWallet2.signTypedData(
      domain,
      { TransferLocked: types.TransferLocked },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        lockId: 1,
        amount: parseUsdt("0.5"),
      }
    );

    // Check balances before
    const balance1Before = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.transferFromLock(
      userWallet1.address,
      userWallet2.address,
      1,
      parseUsdt("0.5"),
      signature
    );
    await tx.wait();


  });

  it("The user should be able to unlock the remaining locked funds after expiry", async function () {
    // Fast forward time by 2 hours
    await ethers.provider.send("evm_increaseTime", [2 * 3600]);
    await ethers.provider.send("evm_mine", []);

    await accounting.unlockSingleLock(userWallet1.address, 1);

    const userLocks = await accounting.getUserLocks(userWallet1.address);
    expect(userLocks.length).to.equal(0);

  });

  it("The user shouldn't be able to create more than 10 locks", async function () {
    const expiry = await getBlockTimestamp() + 3600; // 1 hour from now

    for (let i = 0; i < 10; i++) {
      const signature = await userWallet1.signTypedData(
        domain,
        { Lock: types.Lock },
        {
          userAddress: userWallet1.address,
          serviceAddress: userWallet2.address,
          tokenId: TEST_TOKEN.tokenId,
          amount: parseUsdt("0.1"),
          expiry: expiry + i
        }
      );

      // Submit the transfer to Accounting contract
      const tx = await accounting.createLock(
        userWallet1.address,
        userWallet2.address,
        TEST_TOKEN.tokenId,
        parseUsdt("0.1"),
        expiry + i,
        signature
      );
      await tx.wait();
    }

    const userLocks = await accounting.getUserLocks(userWallet1.address);
    expect(userLocks.length).to.equal(10);

    // Try to create the 11th lock, should fail
    const signature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        userAddress: userWallet1.address,
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("0.1"),
        expiry: expiry + 11
      }
    );

    await expect(accounting.createLock(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("0.1"),
      expiry + 11,
      signature
    )).to.be.revertedWithCustomError(accounting, "TooManyActiveLocks");
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

describe('ModifyLock', function () {
  let accounting: MockAccounting;
  let mockShoyubashi: MockShoyuBashi;
  let provethVerifier: ProvethVerifier;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;

  before(async () => {
    const provider = ethers.provider;
    const [deployer] = await ethers.getSigners();

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
      { kind: 'uups', initializer: 'initialize' }
    ) as unknown as MockAccounting;
    await accounting.waitForDeployment();

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

    const lockSignature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        userAddress: userWallet1.address,
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("1"),
        expiry
      }
    );

    await accounting.createLock(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("1"),
      expiry,
      lockSignature
    );

    const balanceBefore = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const locksBefore = await accounting.getUserLocks(userWallet1.address);
    const lockId = locksBefore[0][0];
    expect(locksBefore[0][3]).to.equal(parseUsdt("1"));

    const newExpiry = expiry + 7200;
    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("2"),
        newExpiry
      }
    );

    const tx = await accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("2"),
      newExpiry,
      modifyLockSignature
    );
    await tx.wait();

    const balanceAfter = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const locksAfter = await accounting.getUserLocks(userWallet1.address);

    expect(balanceAfter).to.equal(balanceBefore - parseUsdt("2"));
    expect(locksAfter[0][3]).to.equal(parseUsdt("3"));
    expect(locksAfter[0][4]).to.equal(newExpiry);
  });

  it("User should be able to add funds while keeping the same expiry", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address);
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("0.5"),
        newExpiry: currentExpiry
      }
    );

    const lockAmountBefore = locks[0][3];

    await accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("0.5"),
      currentExpiry,
      modifyLockSignature
    );

    const locksAfter = await accounting.getUserLocks(userWallet1.address);
    expect(locksAfter[0][3]).to.equal(lockAmountBefore + parseUsdt("0.5"));
    expect(locksAfter[0][4]).to.equal(currentExpiry);
  });

  it("User should be able to extend expiry without adding funds", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address);
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const newExpiry = currentExpiry + 3600;
    const lockAmountBefore = locks[0][3];

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: 0,
        newExpiry
      }
    );

    const balanceBefore = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

    await accounting.modifyLock(
      userWallet1.address,
      lockId,
      0,
      newExpiry,
      modifyLockSignature
    );

    const balanceAfter = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const locksAfter = await accounting.getUserLocks(userWallet1.address);

    expect(balanceAfter).to.equal(balanceBefore);
    expect(locksAfter[0][3]).to.equal(lockAmountBefore);
    expect(locksAfter[0][4]).to.equal(newExpiry);
  });

  it("Should reject modifyLock with invalid lock ID", async function () {
    const expiry = await getBlockTimestamp() + 3600;

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: 999,
        amount: parseUsdt("1"),
        newExpiry: expiry
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      999,
      parseUsdt("1"),
      expiry,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidLockId");
  });

  it("Should reject modifyLock with earlier expiry", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address);
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const earlierExpiry = currentExpiry - 1000;

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("1"),
        newExpiry: earlierExpiry
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("1"),
      earlierExpiry,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidExpiry");
  });

  it("Should reject modifyLock with zero amount and same expiry (no-op)", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address);
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: 0,
        newExpiry: currentExpiry
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      0,
      currentExpiry,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidAmount");
  });

  it("Should reject modifyLock with insufficient balance", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address);
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("1000000"),
        newExpiry: currentExpiry
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("1000000"),
      currentExpiry,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InsufficientBalance");
  });

  it("Should reject modifyLock with wrong signer", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address);
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];

    const modifyLockSignature = await userWallet2.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("0.1"),
        newExpiry: currentExpiry
      }
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("0.1"),
      currentExpiry,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "InvalidSignature");
  });

  it("Should reject replay of modifyLock signature", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address);
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const newExpiry = currentExpiry + 100;

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        userAddress: userWallet1.address,
        lockId: lockId,
        amount: parseUsdt("0.1"),
        newExpiry
      }
    );

    await accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("0.1"),
      newExpiry,
      modifyLockSignature
    );

    await expect(accounting.modifyLock(
      userWallet1.address,
      lockId,
      parseUsdt("0.1"),
      newExpiry,
      modifyLockSignature
    )).to.be.revertedWithCustomError(accounting, "UsedSignature");
  });

  it("Service should still be able to transfer from lock after funds are added", async function () {
    const locks = await accounting.getUserLocks(userWallet1.address);
    const lockId = locks[0][0];
    const lockAmount = locks[0][3];

    const signature = await userWallet2.signTypedData(
      domain,
      { TransferLocked: types.TransferLocked },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        lockId: lockId,
        amount: parseUsdt("0.5"),
      }
    );

    const balance2Before = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

    await accounting.transferFromLock(
      userWallet1.address,
      userWallet2.address,
      lockId,
      parseUsdt("0.5"),
      signature
    );

    const balance2After = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);
    const locksAfter = await accounting.getUserLocks(userWallet1.address);

    expect(balance2After).to.equal(balance2Before + parseUsdt("0.5"));
    expect(locksAfter[0][3]).to.equal(lockAmount - parseUsdt("0.5"));
  });
});

describe('Upgradability', function () {
  let accounting: MockAccounting;
  let mockShoyubashi: MockShoyuBashi;
  let provethVerifier: ProvethVerifier;
  let proxyAddress: string;

  before(async () => {
    const [deployer] = await ethers.getSigners();

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
      { kind: 'uups', initializer: 'initialize' }
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
    const balanceBefore = await accounting.balances(user.address, TEST_TOKEN.tokenId);
    const evmAddressBefore = await accounting.evmAddress();
    const ownerBefore = await accounting.owner();
    const transferNonceBefore = await accounting.transferNonces(user.address);
    const withdrawalNonceBefore = await accounting.withdrawalNonces(user.address);
    const gasPriceBefore = await accounting.gasPrices(testChainId);
    const tokenInfoBefore = await accounting.tokens(TEST_TOKEN.tokenId);
    expect(balanceBefore).to.equal(initialBalance);

    // Upgrade to the same implementation (simulates an upgrade)
    const AccountingV2Factory = await ethers.getContractFactory('MockAccounting');
    const upgraded = await upgrades.upgradeProxy(proxyAddress, AccountingV2Factory, {
      kind: 'uups'
    }) as unknown as MockAccounting;

    // Verify state is preserved after upgrade
    const balanceAfter = await upgraded.balances(user.address, TEST_TOKEN.tokenId);
    const evmAddressAfter = await upgraded.evmAddress();
    const ownerAfter = await upgraded.owner();
    const transferNonceAfter = await upgraded.transferNonces(user.address);
    const withdrawalNonceAfter = await upgraded.withdrawalNonces(user.address);
    const gasPriceAfter = await upgraded.gasPrices(testChainId);
    const tokenInfoAfter = await upgraded.tokens(TEST_TOKEN.tokenId);

    expect(balanceAfter).to.equal(initialBalance, "Balance should be preserved after upgrade");
    expect(evmAddressAfter).to.equal(evmAddressBefore, "EVM address should be preserved after upgrade");
    expect(ownerAfter).to.equal(ownerBefore, "Owner should be preserved after upgrade");
    expect(transferNonceAfter).to.equal(transferNonceBefore, "Transfer nonce should be preserved after upgrade");
    expect(withdrawalNonceAfter).to.equal(withdrawalNonceBefore, "Withdrawal nonce should be preserved after upgrade");
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
        kind: 'uups'
      })
    ).to.be.revertedWithCustomError(accounting, "OwnableUnauthorizedAccount");
  });

  it("Should prevent re-initialization", async function () {
    const [deployer] = await ethers.getSigners();

    await expect(
      accounting.initialize(await mockShoyubashi.getAddress(), await provethVerifier.getAddress(), deployer.address)
    ).to.be.revertedWithCustomError(accounting, "InvalidInitialization");
  });
});
