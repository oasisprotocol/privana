import { expect } from 'chai';
import { ethers, config, upgrades } from 'hardhat';
import { keccak256, Wallet } from 'ethers';
import { MockAccounting, MockAccountingSigner, MockAccountingV2, MockSiweAuth } from '../typechain-types';
import { HardhatNetworkHDAccountsConfig } from 'hardhat/types';
import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import { deployMockAccounting, getDeployer, MOCK_ROFL_APP_ID, mockAuthToken } from './utils';

// Mirrors of the Solidity enums in contracts/Types.sol. Typechain exposes enum
// parameters as uint8 at the TS boundary, so we use ordinals — kept in sync with
// the enum declaration order in Types.sol.
const ChainType = { EVM: 0 } as const;
const TokenType = { NativeEVM: 0, ERC20: 1 } as const;

async function accountingSigner(accounting: MockAccounting): Promise<MockAccountingSigner> {
  return (await ethers.getContractFactory('MockAccountingSigner')).attach(await accounting.signer()) as unknown as MockAccountingSigner;
}

const types = {
  Lock: [
    { name: "serviceAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "expiry", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
  ModifyLock: [
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
    { name: "toAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
  Withdraw: [
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
}

const TEST_TOKEN = {
  tokenType: TokenType.ERC20,
  // keccak256(abi.encodePacked(uint256(84532), address(0x036cbd53842c5426634e7929541ec2318f3dcf7e)))
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

type UpgradeDeployment = {
  deploymentTransaction?: () => { wait(): Promise<unknown> } | null;
  deployTransaction?: { wait(): Promise<unknown> };
};

async function waitForUpgradeTx(contract: UpgradeDeployment): Promise<void> {
  const deploymentTx =
    typeof contract.deploymentTransaction === "function"
      ? contract.deploymentTransaction()
      : undefined;
  const tx = deploymentTx ?? contract.deployTransaction;
  if (tx) {
    await tx.wait();
  }
}

describe('Accounting', function () {
  let accounting: MockAccounting;
  let mockSiweAuth: MockSiweAuth;
  let accountingUser1: MockAccounting;
  let accountingUser2: MockAccounting;
  let user1: HardhatEthersSigner;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;
  let tokenId: string;

  before(async () => {
    const [user1, user2, service] = (await ethers.getSigners()).slice(1, 4);
    const deployer = getDeployer();

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth', deployer);
    mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    accounting = await deployMockAccounting(await mockSiweAuth.getAddress());
    accountingUser1 = accounting.connect(user1) as MockAccounting;
    accountingUser2 = accounting.connect(user2) as MockAccounting;

    const hdNodeWallet = ethers.HDNodeWallet.fromPhrase(
      (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig).mnemonic,
    );

    // Drive index 0 and 1 wallets
    userWallet1 = hdNodeWallet.connect(ethers.provider) as any;
    userWallet2 = hdNodeWallet.derivePath("44'/60'/0'/0/0").connect(ethers.provider) as any;
    const userLocks = await accounting.getUserLocks(mockAuthToken(userWallet1.address));

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
    const tx1 = await accounting.setTokenInfo({
      tokenType: TEST_TOKEN.tokenType,
      data: data
    });
    await tx1.wait();

    // Set gas price for withdrawal tests
    const tx2 = await accounting.setGasPrice(TEST_TOKEN.chainId, 1000000000n); // 1 gwei
    await tx2.wait();

    tokenId = TEST_TOKEN.tokenId;
  });

  async function ensurePrivacyScenario(): Promise<void> {
    const ownerBal = await accounting.getBalance(userWallet1.address, tokenId);
    const callerBal = await accounting.getBalance(userWallet2.address, tokenId);
    const ownerLocks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));

    if (
      ownerBal === parseUsdt("8") &&
      callerBal === parseUsdt("1") &&
      ownerLocks.length === 1 &&
      ownerLocks[0][1].toLowerCase() === userWallet2.address.toLowerCase() &&
      ownerLocks[0][2] === tokenId &&
      ownerLocks[0][3] === parseUsdt("1")
    ) {
      return;
    }

    if (ownerBal !== 0n || callerBal !== 0n || ownerLocks.length !== 0) {
      throw new Error("Unexpected privacy test preconditions");
    }

    await accounting.setBalance(userWallet1.address, tokenId, parseUsdt("9"));
    await accounting.setBalance(userWallet2.address, tokenId, parseUsdt("1"));

    const expiry = (await getBlockTimestamp()) + 3600;
    const lockNonce = await accounting.createLockNonces(userWallet1.address);
    const signature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        serviceAddress: userWallet2.address,
        tokenId,
        amount: parseUsdt("1"),
        expiry,
        nonce: lockNonce,
      }
    );

    await accounting.createLock(
      userWallet2.address,
      tokenId,
      parseUsdt("1"),
      expiry,
      lockNonce,
      signature
    );
  }

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

    expect(tokenId).to.equal(TEST_TOKEN.tokenId);
    expect(await accounting.decodeEVMErc20TokenData(data)).to.deep.equal([TEST_TOKEN.chainId, TEST_TOKEN.address]);
    expect(await accounting.decodedErc20TokenAddressWord(data)).to.equal(BigInt(TEST_TOKEN.address));
  });

  it("Set up initial balance via setBalance", async function () {
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt("10"));
    const balance = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    expect(balance).to.equal(parseUsdt("10"));
  });

  it("Test EIP712 transfer", async function () {
    const nonce = await accounting.transferNonces(userWallet1.address);
    const signature = await userWallet1.signTypedData(
      domain,
      { Transfer: types.Transfer },
      {
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

    // Submit the lock to Accounting contract
    const tx = await accounting.createLock(
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
    const userLocks = await accounting.getUserLocks(mockAuthToken(userWallet1.address));

    expect(userLocks.length).to.equal(1);
    expect(userLocks[0][1]).to.equal(userWallet2.address);
    expect(userLocks[0][2]).to.equal(TEST_TOKEN.tokenId);
    expect(userLocks[0][3]).to.equal(parseUsdt("1"));
    expect(userLocks[0][4]).to.be.equal(expiry);
  });

  it('Privacy: view functions derive identity from auth token, so user2 cannot read user1 data', async function () {
    await ensurePrivacyScenario();

    const ownerBal = await accountingUser1.balanceOf(tokenId, mockAuthToken(userWallet1.address));
    const callerBal = await accountingUser2.balanceOf(tokenId, mockAuthToken(userWallet2.address));
    expect(ownerBal).to.equal(parseUsdt("8"));
    expect(callerBal).to.equal(parseUsdt("1"));
    expect(callerBal).to.not.equal(ownerBal);

    const ownerLocks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const callerLocks = await accountingUser2.getUserLocks(mockAuthToken(userWallet2.address));
    expect(ownerLocks.length).to.equal(1);
    expect(callerLocks.length).to.equal(0);
  });

  it('Privacy: user-only view functions should return correct data for the owner', async function () {
    await ensurePrivacyScenario();

    const bal = await accountingUser1.balanceOf(tokenId, mockAuthToken(userWallet1.address));
    expect(bal).to.equal(parseUsdt("8"));

    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    expect(locks).to.be.an('array');
    expect(locks.length).to.equal(1);
    expect(locks[0][1]).to.equal(userWallet2.address);
    expect(locks[0][2]).to.equal(tokenId);
    expect(locks[0][3]).to.equal(parseUsdt("1"));
  });

  it('Privacy: non-empty auth tokens determine the private-read subject', async function () {
    await ensurePrivacyScenario();

    const authToken = ethers.AbiCoder.defaultAbiCoder().encode(
      ['address'],
      [userWallet1.address]
    );

    const bal = await accountingUser2.balanceOf(tokenId, authToken);
    expect(bal).to.equal(parseUsdt("8"));

    const locks = await accountingUser2.getUserLocks(authToken);
    expect(locks.length).to.equal(1);
    expect(locks[0][1]).to.equal(userWallet2.address);
  });

  it('Privacy: service-scoped total locked balance should only count the caller\'s locks', async function () {
    await ensurePrivacyScenario();

    const serviceLocks = await accountingUser2.getServiceLocks(userWallet1.address, mockAuthToken(userWallet2.address));
    expect(serviceLocks.length).to.equal(1);
    expect(serviceLocks[0][1]).to.equal(userWallet2.address);
    expect(serviceLocks[0][2]).to.equal(tokenId);
    expect(serviceLocks[0][3]).to.equal(parseUsdt("1"));
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
    const network = await ethers.provider.getNetwork();
    if (network.chainId >= 0x5afd && network.chainId <= 0x5aff) {
      this.skip();
    }

    // Fast forward time by 2 hours
    await ethers.provider.send("evm_increaseTime", [2 * 3600]);
    await ethers.provider.send("evm_mine", []);

    const tx = await accounting.unlockSingleLock(userWallet1.address, 1);
    await tx.wait();

    const userLocks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    expect(userLocks.length).to.equal(0);

  });

  it("The user shouldn't be able to create more than 10 locks", async function () {
    const expiry = await getBlockTimestamp() + 3600; // 1 hour from now

    const fundTx = await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt("2.0"));
    await fundTx.wait();

    const userLocksBefore = await accounting.getUserLocks(mockAuthToken(userWallet1.address));
    for (let i = 0; i < 10-userLocksBefore.length; i++) {
      const lockNonce = await accounting.createLockNonces(userWallet1.address);
      const signature = await userWallet1.signTypedData(
        domain,
        { Lock: types.Lock },
        {
          serviceAddress: userWallet2.address,
          tokenId: TEST_TOKEN.tokenId,
          amount: parseUsdt("0.1"),
          expiry: expiry + i,
          nonce: lockNonce
        }
      );

      const tx = await accounting.createLock(
        userWallet2.address,
        TEST_TOKEN.tokenId,
        parseUsdt("0.1"),
        expiry + i,
        lockNonce,
        signature
      );
      await tx.wait();
    }

    const userLocks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    expect(userLocks.length).to.equal(10);

    // Try to create the 11th lock, should fail
    const lockNonce = await accounting.createLockNonces(userWallet1.address);
    const signature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("0.1"),
        expiry: expiry + 11,
        nonce: lockNonce
      }
    );

    await expect(accounting.createLock(
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("0.1"),
      expiry + 11,
      lockNonce,
      signature
    )).to.be.reverted; // WithCustomError(accounting, "TooManyActiveLocks"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
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
        serviceAddress: userWallet1.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("0.5"),
        expiry,
        nonce: lockNonce,
      }
    );

    const tx = await accounting.createLock(
      userWallet1.address, TEST_TOKEN.tokenId,
      parseUsdt("0.5"), expiry, lockNonce, signature
    );
    await tx.wait()

    await expect(accounting.createLock(
      userWallet1.address, TEST_TOKEN.tokenId,
      parseUsdt("0.5"), expiry, lockNonce, signature
    )).to.be.reverted; // WithCustomError(accounting, "InvalidNonce"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });


  it("User should be able to withdraw TEST token using EIP712 signature", async function () {
    const fundTx = await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt("0.15"));
    await fundTx.wait();

    const nonce = await accounting.withdrawalNonces(userWallet1.address);
    const signature = await userWallet1.signTypedData(
      domain,
      { Withdraw: types.Withdraw },
      {
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("0.1"),
        nonce: nonce,
      }
    );

    // Submit the withdrawal request to Accounting contract
    const tx = await accounting.requestWithdrawal(
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

    // Requires Sapphire EIP155Signer precompile.
    const network = await ethers.provider.getNetwork();
    if ((0x5afd <= network.chainId) && (network.chainId <= 0x5aff)) {
      const tx2 = await accounting.resolveWithdrawal(0);
      const receipt2 = await tx2.wait();
      const withdrawalAfter = await accounting.withdrawals(0);
      expect(withdrawalAfter.resolved).to.equal(true);
    }
  });

  describe("creditDeposit (via mock)", function () {
    it("should credit deposit to beneficiary", async function () {
      const depositId = keccak256(ethers.AbiCoder.defaultAbiCoder().encode(
        ["uint256", "bytes32", "bytes32", "uint256"],
        [84532, ethers.id("0xtxhash1"), tokenId, 0]
      ));
      const amount = parseUsdt("100");
      await accounting.mockCreditDeposit(userWallet1.address, tokenId, amount, depositId);
      const balance = await accounting.getBalance(userWallet1.address, tokenId);
      expect(balance).to.be.gte(amount);
    });

    it("should reject duplicate deposit key", async function () {
      const depositId = keccak256(ethers.AbiCoder.defaultAbiCoder().encode(
        ["uint256", "bytes32", "bytes32", "uint256"],
        [84532, ethers.id("0xtxhash-dedup"), tokenId, 0]
      ));
      await accounting.mockCreditDeposit(userWallet1.address, tokenId, parseUsdt("50"), depositId);
      await expect(
        accounting.mockCreditDeposit(userWallet1.address, tokenId, parseUsdt("50"), depositId)
      ).to.be.reverted; // WithCustomError(accounting, "DepositAlreadyProcessed"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
    });

    it("should reject zero amount", async function () {
      const depositId = keccak256(ethers.AbiCoder.defaultAbiCoder().encode(
        ["uint256", "bytes32", "bytes32", "uint256"],
        [84532, ethers.id("0xtxhash-zero"), tokenId, 0]
      ));
      await expect(
        accounting.mockCreditDeposit(userWallet1.address, tokenId, 0n, depositId)
      ).to.be.reverted// WithCustomError(accounting, "InvalidAmount"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
    });

    it("should reject unregistered token", async function () {
      const fakeTokenId = keccak256(ethers.AbiCoder.defaultAbiCoder().encode(
        ["uint8", "bytes"],
        [1, ethers.AbiCoder.defaultAbiCoder().encode(["uint256", "address"], [84532, ethers.ZeroAddress])]
      ));
      const depositId = keccak256(ethers.AbiCoder.defaultAbiCoder().encode(
        ["uint256", "bytes32", "bytes32", "uint256"],
        [84532, ethers.id("0xtxhash-unreg"), fakeTokenId, 0]
      ));
      await expect(
        accounting.mockCreditDeposit(userWallet1.address, fakeTokenId, parseUsdt("100"), depositId)
      ).to.be.reverted; // WithCustomError(accounting, "UnsupportedTokenType"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
    });
  });

  describe("getDepositAddress", function () {
    it("should return deterministic deposit address for beneficiary", async function () {
      // Requires Sapphire EIP155Signer precompile.
      const network = await ethers.provider.getNetwork();
      if (network.chainId < 0x5afd || network.chainId > 0x5aff) {
        this.skip();
      }

      const addr1 = await accounting.getDepositAddress(ChainType.EVM, 0, mockAuthToken(userWallet1.address));
      expect(addr1).to.equal("0xC739394587942f984FF3e5E28FEaA96a88D52E97");

      const addr2 = await accounting.getDepositAddress(ChainType.EVM, 0, mockAuthToken(userWallet2.address));
      expect(addr2).to.equal("0xE6bB861D91d5d84A2278c4f88E899bf1b5Af91b8");
    });
  });

  describe("emergencyWithdraw", function () {
    it("should create emergency withdraw request", async function () {
      const user = await ethers.getSigner(userWallet1.address);
      const expectedId = await accounting.emergencyWithdrawKey(userWallet1.address, tokenId, 0);

      await expect(
        accounting.connect(user).requestEmergencyWithdraw(tokenId, userWallet1.address, 0)
      ).to.emit(accounting, "EmergencyWithdrawRequested").withArgs(expectedId, tokenId);

      const req = await accounting.emergencyWithdrawRequests(expectedId);
      expect(req.toAddress).to.equal(userWallet1.address);
      expect(req.blockNumber).to.not.equal(0n);
    });

    it("should overwrite request on re-request (implicit cancel)", async function () {
      const user = await ethers.getSigner(userWallet1.address);
      const requestId = await accounting.emergencyWithdrawKey(userWallet1.address, tokenId, 0);

      // Re-request with a different destination — overwrites the prior slot.
      const newDest = userWallet1.address;
      await accounting.connect(user).requestEmergencyWithdraw(tokenId, newDest, 0);

      const req = await accounting.emergencyWithdrawRequests(requestId);
      expect(req.toAddress).to.equal(newDest);
    });

    it("should isolate requests by (beneficiary, tokenId, version)", async function () {
      const keyA = await accounting.emergencyWithdrawKey(userWallet1.address, tokenId, 0);
      const keyB = await accounting.emergencyWithdrawKey(userWallet1.address, tokenId, 1);
      const keyC = await accounting.emergencyWithdrawKey(userWallet2.address, tokenId, 0);
      expect(keyA).to.not.equal(keyB);
      expect(keyA).to.not.equal(keyC);
      expect(keyB).to.not.equal(keyC);
    });

    it("should execute emergency withdraw after 1-block delay (ERC20)", async function () {
      // executeEmergencyWithdraw calls EIP155Signer.sign() which needs Sapphire precompiles.
      // On Hardhat we verify state transitions up to the signing step; on Sapphire we get signedTx.
      const user1 = (await ethers.getSigners())[1];
      const tx1 = await accountingUser1.requestEmergencyWithdraw(tokenId, userWallet2.address, 0);
      await tx1.wait();

      // Requires Sapphire EIP155Signer precompile.
      const network = await ethers.provider.getNetwork();
      if (network.chainId < 0x5afd || network.chainId > 0x5aff) {
        await expect(
          accountingUser1.executeEmergencyWithdraw(
            user1.address, tokenId, 0, 0, parseUsdt("1"), 1000000000n
          )
        ).to.be.reverted;
        this.skip();
      }

      // Full happy path on Sapphire: decode signedTx and verify EIP-155 chainId matches
      // the chainId encoded in tokens[tokenId].data. Regression test for the issue where
      // caller-supplied chainId could produce a signed tx for the wrong EVM chain.
      const signedTx: string = await accounting.executeEmergencyWithdraw.staticCall(
        user1.address, tokenId, 0, 0, parseUsdt("1"), 1000000000n
      );
      expect(ethers.Transaction.from(signedTx).chainId).to.equal(BigInt(TEST_TOKEN.chainId));

      const tx = await accounting.executeEmergencyWithdraw(
        user1.address, tokenId, 0, 0, parseUsdt("1"), 1000000000n
      );
      await tx.wait();
      const requestId = await accounting.emergencyWithdrawKey(user1.address, tokenId, 0);
      await expect(tx).to.emit(accounting, "EmergencyWithdrawExecuted").withArgs(requestId);
    });

    it("should reject request with unregistered tokenId", async function () {
      // The new UnsupportedTokenType pre-check in requestEmergencyWithdraw prevents
      // storing a request for a token the contract doesn't know about.
      const user = await ethers.getSigner(userWallet1.address);
      const unknownTokenId = "0x" + "11".repeat(32);
      await expect(
        accounting.connect(user).requestEmergencyWithdraw(unknownTokenId, userWallet1.address, 0)
      ).to.be.reverted; // WithCustomError(accounting, "UnsupportedTokenType"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
    });

    it("should reject execute on non-existent request", async function () {
      // Unused (beneficiary, version) pair — slot is empty.
      await expect(
        accounting.connect(await ethers.getSigner(userWallet1.address)).executeEmergencyWithdraw(
          userWallet1.address, tokenId, 99, 0, parseUsdt("1"), 1000000000n
        )
      ).to.be.reverted; // WithCustomError(accounting, "EmergencyWithdrawNotFound"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
    });

    // Note: EmergencyWithdrawTooSoon requires request + execute in the same block,
    // which needs evm_setAutomine(false). Hardhat's batch-mining semantics are finicky
    // for revert assertions — tested manually on Sapphire testnet instead.
  });

  describe("roflSignerAddress (signed-query auth)", function () {
    it("should start unset and reject view-signing calls with RoflSignerNotSet", async function () {
      // Ensure unset state by deploying a fresh proxy — the parent `before` does not set it.
      const fresh = await deployMockAccounting(await mockSiweAuth.getAddress());
      expect(await fresh.roflSignerAddress()).to.equal(ethers.ZeroAddress);

      await expect(
        fresh.generateGasFundingTx.staticCall(userWallet1.address, 84532, 10n, 0n, 1000000000n)
      ).to.be.reverted; // WithCustomError(fresh, "RoflSignerNotSet"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
    });

    it("should update roflSignerAddress and emit RoflSignerUpdated", async function () {
      const signer = userWallet1.address;
      const signerContract = await accountingSigner(accounting);
      const tx = await accounting.mockSetRoflSignerAddress(signer);
      await expect(tx)
        .to.emit(accounting, "RoflSignerUpdated")
        .withArgs(signer);
      await expect(tx)
        .to.emit(signerContract, "RoflSignerUpdated")
        .withArgs(signer);
      expect(await accounting.roflSignerAddress()).to.equal(signer);
    });

    it("should update gas price through Accounting and emit GasPriceSet there", async function () {
      const chainId = 84533;
      const gasPrice = 2000000000n;
      await expect(accounting.setGasPrice(chainId, gasPrice))
        .to.emit(accounting, "GasPriceSet")
        .withArgs(chainId, gasPrice);
      expect(await accounting.gasPrices(chainId)).to.equal(gasPrice);
    });

    it("should preserve gas limit ABI getters on Accounting", async function () {
      expect(await accounting.gasLimitNativeSweep()).to.equal(21000n);
      expect(await accounting.gasLimitERC20Sweep()).to.equal(65000n);
      expect(await accounting.gasLimitNativeWithdraw()).to.equal(50000n);
      expect(await accounting.gasLimitERC20Withdraw()).to.equal(100000n);
    });

    it("should reject zero address in setter", async function () {
      await expect(accounting.mockSetRoflSignerAddress(ethers.ZeroAddress))
        .to.be.reverted; // WithCustomError(accounting, "InvalidAddress"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
    });

    it("real setRoflSignerAddress is gated by onlyROFL", async function () {
      // Non-mock setter must fail when the ROFL precompile can't authenticate the caller.
      // On Hardhat the Sapphire precompile doesn't exist, so the call reverts — which is
      // exactly the guarantee the modifier provides. If the gate were removed, the call
      // would succeed (since the inner body is a plain storage write).
      await expect(
        accounting.setRoflSignerAddress(userWallet1.address)
      ).to.be.reverted;
    });

    it("should reject direct signer setRoflSignerAddress calls", async function () {
      const signerContract = await accountingSigner(accounting);
      await expect(
        signerContract.setRoflSignerAddress(userWallet1.address)
      ).to.be.revertedWithCustomError(signerContract, "NotAccounting");
    });

    it("should reject direct signer setGasPrice calls", async function () {
      const signerContract = await accountingSigner(accounting);
      await expect(
        signerContract.setGasPrice(84534, 2000000000n)
      ).to.be.revertedWithCustomError(signerContract, "NotAccounting");
    });

    it("should reject invalid signer addresses", async function () {
      await expect(
        accounting.setSigner(ethers.ZeroAddress)
      ).to.be.revertedWithCustomError(accounting, "InvalidSigner");
      await expect(
        accounting.setSigner(userWallet1.address)
      ).to.be.revertedWithCustomError(accounting, "InvalidSigner");
    });

    it("should reject a signer linked to another accounting proxy", async function () {
      const otherAccounting = await deployMockAccounting(await mockSiweAuth.getAddress());
      await expect(
        accounting.setSigner(await otherAccounting.signer())
      ).to.be.revertedWithCustomError(accounting, "InvalidSigner");
    });

    it("should reject a signer owned by a different admin", async function () {
      const deployer = getDeployer();
      const otherOwner = getDeployer(1);
      const MockAccountingSignerFactory = await ethers.getContractFactory('MockAccountingSigner', deployer);
      const signerContract = await upgrades.deployProxy(
        MockAccountingSignerFactory,
        [otherOwner.address, await accounting.getAddress()],
        {
          kind: 'uups',
          initializer: 'initialize',
          unsafeAllow: ['constructor', 'state-variable-immutable'],
        }
      ) as unknown as MockAccountingSigner;
      await signerContract.waitForDeployment();

      await expect(
        accounting.setSigner(await signerContract.getAddress())
      ).to.be.revertedWithCustomError(accounting, "InvalidSigner");
    });

    it("should route signer ownership through Accounting ownership transfer", async function () {
      const otherOwner = getDeployer(1);
      const fresh = await deployMockAccounting(await mockSiweAuth.getAddress());
      const signerContract = await accountingSigner(fresh);

      await expect(
        signerContract.transferOwnership(otherOwner.address)
      ).to.be.revertedWithCustomError(signerContract, "NotAccounting");

      const transferTx = await fresh.transferOwnership(otherOwner.address);
      await transferTx.wait();

      expect(await signerContract.owner()).to.equal(otherOwner.address);
      expect(await fresh.owner()).to.equal(otherOwner.address);
    });

    it("should reject invalid history module addresses", async function () {
      await expect(
        accounting.setHistoryModule(ethers.ZeroAddress)
      ).to.be.revertedWithCustomError(accounting, "InvalidHistoryModule");
      await expect(
        accounting.setHistoryModule(userWallet1.address)
      ).to.be.revertedWithCustomError(accounting, "InvalidHistoryModule");
    });

    it("should set history and signer links atomically", async function () {
      const deployer = getDeployer();
      const AccountingHistoryModuleFactory = await ethers.getContractFactory('AccountingHistoryModule');
      const MockAccountingSignerFactory = await ethers.getContractFactory('MockAccountingSigner', deployer);

      const historyModule = await AccountingHistoryModuleFactory.deploy();
      await historyModule.waitForDeployment();
      const signerContract = await upgrades.deployProxy(
        MockAccountingSignerFactory,
        [deployer.address, await accounting.getAddress()],
        {
          kind: 'uups',
          initializer: 'initialize',
          unsafeAllow: ['constructor', 'state-variable-immutable'],
        }
      ) as unknown as MockAccountingSigner;
      await signerContract.waitForDeployment();

      const historyAddress = await historyModule.getAddress();
      const signerAddress = await signerContract.getAddress();

      const tx = await accounting.setModules(
        historyAddress,
        signerAddress
      );
      await tx.wait();

      expect(await accounting.historyModule()).to.equal(historyAddress);
      expect(await accounting.signer()).to.equal(signerAddress);
    });

    it("should allow the ROFL signer to generate sweep and gas funding txs through Accounting", async function () {
      await accounting.mockSetRoflSignerAddress(userWallet1.address);
      const abiCoder = ethers.AbiCoder.defaultAbiCoder();

      const nativeSweep = await accounting.connect(userWallet1).generateSweepNativeTransfer.staticCall(
        userWallet1.address,
        ChainType.EVM,
        1n,
        84532,
        10n,
        0n,
        1000000000n
      );
      const decodedNativeSweep = abiCoder.decode(
        ['string', 'address', 'uint8', 'uint256', 'uint256', 'uint256', 'uint64', 'uint256'],
        nativeSweep
      );
      expect(decodedNativeSweep[0]).to.equal('native-sweep');
      expect(decodedNativeSweep[1]).to.equal(userWallet1.address);
      expect(decodedNativeSweep[2]).to.equal(BigInt(ChainType.EVM));

      const erc20Sweep = await accounting.connect(userWallet1).generateSweepERC20Transfer.staticCall(
        userWallet1.address,
        ChainType.EVM,
        1n,
        84532,
        userWallet2.address,
        10n,
        0n,
        1000000000n
      );
      const decodedErc20Sweep = abiCoder.decode(
        ['string', 'address', 'uint8', 'uint256', 'uint256', 'address', 'uint256', 'uint64', 'uint256'],
        erc20Sweep
      );
      expect(decodedErc20Sweep[0]).to.equal('erc20-sweep');
      expect(decodedErc20Sweep[1]).to.equal(userWallet1.address);
      expect(decodedErc20Sweep[5]).to.equal(userWallet2.address);

      const gasFunding = await accounting.connect(userWallet1).generateGasFundingTx.staticCall(
        userWallet2.address,
        84532,
        10n,
        0n,
        1000000000n
      );
      const decodedGasFunding = abiCoder.decode(
        ['string', 'address', 'uint256', 'uint256', 'uint64', 'uint256'],
        gasFunding
      );
      expect(decodedGasFunding[0]).to.equal('gas-funding');
      expect(decodedGasFunding[1]).to.equal(userWallet2.address);
    });

    const wrongSenderCases = [
      {
        name: "generateSweepNativeTransfer",
        call: (signer: typeof userWallet2) =>
          accounting.connect(signer).generateSweepNativeTransfer.staticCall(
            userWallet1.address, ChainType.EVM, 1n, 84532, 10n, 0n, 1000000000n
          ),
      },
      {
        name: "generateSweepERC20Transfer",
        call: (signer: typeof userWallet2) =>
          accounting.connect(signer).generateSweepERC20Transfer.staticCall(
            userWallet1.address, ChainType.EVM, 1n, 84532, userWallet2.address, 10n, 0n, 1000000000n
          ),
      },
      {
        name: "generateGasFundingTx",
        call: (signer: typeof userWallet2) =>
          accounting.connect(signer).generateGasFundingTx.staticCall(
            userWallet1.address, 84532, 10n, 0n, 1000000000n
          ),
      },
    ];

    wrongSenderCases.forEach(({ name, call }) => {
      it(`${name} rejects wrong sender with NotAuthorizedROFL`, async function () {
        await accounting.mockSetRoflSignerAddress(userWallet1.address);
        await expect(call(userWallet2)).to.be.revertedWithCustomError(
          accounting, "NotAuthorizedROFL"
        );
      });
    });
  });

});

describe('WithdrawFromLock', function () {
  let accounting: MockAccounting;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;
  let userWallet3: Wallet;

  const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

  before(async () => {
    const provider = ethers.provider;
    const mnemonic = (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig).mnemonic;

    userWallet1 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/0")
      .connect(provider) as any;
    userWallet2 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/1")
      .connect(provider) as any;
    userWallet3 = ethers.HDNodeWallet
      .fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/2")
      .connect(provider) as any;
  });

  beforeEach(async () => {
    const [deployer] = await ethers.getSigners();

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
    const mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    accounting = await deployMockAccounting(await mockSiweAuth.getAddress());

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
    const lockNonce = await accounting.createLockNonces(userWallet1.address);
    const lockSignature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        serviceAddress,
        tokenId: TEST_TOKEN.tokenId,
        amount: lockAmount,
        expiry,
        nonce: lockNonce,
      }
    );

    await accounting.createLock(
      serviceAddress,
      TEST_TOKEN.tokenId,
      lockAmount,
      expiry,
      lockNonce,
      lockSignature
    );

    const userLocks = await accounting.connect(userWallet1).getUserLocks(mockAuthToken(userWallet1.address));
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

    const locksAfter = await accounting.connect(userWallet1).getUserLocks(mockAuthToken(userWallet1.address));
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
    ).to.be.reverted; // WithCustomError(accounting, "AddressMismatch"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
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
    ).to.be.reverted; // WithCustomError(accounting, "InvalidSignature"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
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
    ).to.be.reverted; // WithCustomError(accounting, "InvalidNonce"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

});

describe('ModifyLock', function () {
  let accounting: MockAccounting;
  let mockSiweAuth: MockSiweAuth;
  let accountingUser1: MockAccounting;
  let accountingUser2: MockAccounting;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;

  const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

  before(async () => {
    const provider = ethers.provider;
    const [user1, user2] = (await ethers.getSigners()).slice(1,3);
    const deployer = getDeployer();

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth', deployer);
    mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();
    mockSiweAuth = mockSiweAuth.connect((await ethers.getSigners())[0]); // Use wrapped signer for sending txes.

    accounting = await deployMockAccounting(await mockSiweAuth.getAddress());
    const mnemonic = (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig).mnemonic;
    userWallet1 = ethers.HDNodeWallet.fromPhrase(
      mnemonic,
      undefined,
      "m/44'/60'/0'/0/0",
    ).connect(provider) as any;
    userWallet2 = ethers.HDNodeWallet.fromPhrase(
      mnemonic,
      undefined,
      "m/44'/60'/0'/0/1",
    ).connect(provider) as any;
    accountingUser1 = accounting.connect(userWallet1) as MockAccounting;
    accountingUser2 = accounting.connect(userWallet2) as MockAccounting;

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

    const tx = await accounting.setTokenInfo({
      tokenType: TEST_TOKEN.tokenType,
      data: data
    });
    await tx.wait();

    // Set up initial balance via setBalance
    await accounting.setBalance(userWallet1.address, TEST_TOKEN.tokenId, parseUsdt("10"));
  });

  it("User should be able to add funds to an existing lock", async function () {
    const expiry = await getBlockTimestamp() + 3600;
    const lockNonce = await accounting.createLockNonces(userWallet1.address);

    const lockSignature = await userWallet1.signTypedData(
      domain,
      { Lock: types.Lock },
      {
        serviceAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("1"),
        expiry,
        nonce: lockNonce
      }
    );

    await accounting.createLock(
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseUsdt("1"),
      expiry,
      lockNonce,
      lockSignature
    );

    const balanceBefore = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const locksBefore = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const lockId = locksBefore[0][0];
    expect(locksBefore[0][3]).to.equal(parseUsdt("1"));

    const newExpiry = expiry + 7200;
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);
    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        lockId: lockId,
        amount: parseUsdt("2"),
        newExpiry,
        nonce: modifyNonce
      }
    );

    const tx = await accounting.modifyLock(
      lockId,
      parseUsdt("2"),
      newExpiry,
      modifyNonce,
      modifyLockSignature
    );
    await tx.wait();

    const balanceAfter = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const locksAfter = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));

    expect(balanceAfter).to.equal(balanceBefore - parseUsdt("2"));
    expect(locksAfter[0][3]).to.equal(parseUsdt("3"));
    expect(locksAfter[0][4]).to.equal(newExpiry);
  });

  it("User should be able to add funds while keeping the same expiry", async function () {
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        lockId: lockId,
        amount: parseUsdt("0.5"),
        newExpiry: currentExpiry,
        nonce: modifyNonce
      }
    );

    const lockAmountBefore = locks[0][3];

    await accounting.modifyLock(
      lockId,
      parseUsdt("0.5"),
      currentExpiry,
      modifyNonce,
      modifyLockSignature
    );

    const locksAfter = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    expect(locksAfter[0][3]).to.equal(lockAmountBefore + parseUsdt("0.5"));
    expect(locksAfter[0][4]).to.equal(currentExpiry);
  });

  it("User should be able to extend expiry without adding funds", async function () {
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const newExpiry = currentExpiry + 3600;
    const lockAmountBefore = locks[0][3];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        lockId: lockId,
        amount: 0,
        newExpiry,
        nonce: modifyNonce
      }
    );

    const balanceBefore = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);

    const tx = await accounting.modifyLock(
      lockId,
      0,
      newExpiry,
      modifyNonce,
      modifyLockSignature
    );
    await tx.wait();

    const balanceAfter = await accounting.getBalance(userWallet1.address, TEST_TOKEN.tokenId);
    const locksAfter = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));

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
        lockId: 999,
        amount: parseUsdt("1"),
        newExpiry: expiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      999,
      parseUsdt("1"),
      expiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.reverted; // WithCustomError(accounting, "InvalidLockId"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

  it("Should reject modifyLock with earlier expiry", async function () {
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const earlierExpiry = currentExpiry - 1000;
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        lockId: lockId,
        amount: parseUsdt("1"),
        newExpiry: earlierExpiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      lockId,
      parseUsdt("1"),
      earlierExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.reverted;; // WithCustomError(accounting, "InvalidExpiry"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

  it("Should reject modifyLock with zero amount and same expiry (no-op)", async function () {
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        lockId: lockId,
        amount: 0,
        newExpiry: currentExpiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      lockId,
      0,
      currentExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.reverted; // WithCustomError(accounting, "InvalidAmount"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

  it("Should reject modifyLock with insufficient balance", async function () {
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        lockId: lockId,
        amount: parseUsdt("1000000"),
        newExpiry: currentExpiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      lockId,
      parseUsdt("1000000"),
      currentExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.reverted; // WithCustomError(accounting, "InsufficientBalance"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

  it("Should derive modifyLock user from signer", async function () {
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const lockId = locks[0][0];
    const currentExpiry = locks[0][4];
    const modifyNonce = await accounting.modifyLockNonces(userWallet2.address);

    const modifyLockSignature = await userWallet2.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        lockId: lockId,
        amount: parseUsdt("0.1"),
        newExpiry: currentExpiry,
        nonce: modifyNonce
      }
    );

    await expect(accounting.modifyLock(
      lockId,
      parseUsdt("0.1"),
      currentExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.reverted; // WithCustomError(accounting, "InvalidLockId"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

  it("Should reject replay of modifyLock signature", async function () {
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
    const lockId = locks[0][0];
    const currentExpiry = Number(locks[0][4]);
    const newExpiry = currentExpiry + 100;
    const modifyNonce = await accounting.modifyLockNonces(userWallet1.address);

    const modifyLockSignature = await userWallet1.signTypedData(
      domain,
      { ModifyLock: types.ModifyLock },
      {
        lockId: lockId,
        amount: parseUsdt("0.1"),
        newExpiry,
        nonce: modifyNonce
      }
    );

    const tx = await accounting.modifyLock(
      lockId,
      parseUsdt("0.1"),
      newExpiry,
      modifyNonce,
      modifyLockSignature
    );
    await tx.wait();

    await expect(accounting.modifyLock(
      lockId,
      parseUsdt("0.1"),
      newExpiry,
      modifyNonce,
      modifyLockSignature
    )).to.be.reverted; // WithCustomError(accounting, "InvalidNonce"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

  it("Service should still be able to transfer from lock after funds are added", async function () {
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
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
    const locksAfter = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));

    expect(balance2After).to.equal(balance2Before + parseUsdt("0.5"));
    expect(locksAfter[0][3]).to.equal(lockAmount - parseUsdt("0.5"));
  });

  it("Should reject replay of transferFromLock signature", async function () {
    // Service (userWallet2) transfers from its lock on userWallet1's account
    const locks = await accountingUser1.getUserLocks(mockAuthToken(userWallet1.address));
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

    const tx1 = await accounting.transferFromLock(
      userWallet1.address, userWallet2.address, lockId,
      parseUsdt("0.1"), transferLockedNonce, signature
    );
    await tx1.wait();

    await expect(accounting.transferFromLock(
      userWallet1.address, userWallet2.address, lockId,
      parseUsdt("0.1"), transferLockedNonce, signature
    )).to.be.reverted; // WithCustomError(accounting, "InvalidNonce"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });
});

describe('Upgradability', function () {
  let accounting: MockAccounting;
  let mockSiweAuth: MockSiweAuth;
  let proxyAddress: string;

  const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

  before(async () => {
    const deployer = getDeployer();

    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth', deployer);
    mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    accounting = await deployMockAccounting(await mockSiweAuth.getAddress());

    proxyAddress = await accounting.getAddress();
  });

  it("Should preserve state after upgrade", async function () {
    const deployer = getDeployer();
    const user = (await ethers.getSigners())[1];

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
      constructorArgs: [await mockSiweAuth.getAddress()],
      unsafeAllow: ['constructor', 'state-variable-immutable', 'delegatecall'],
    }) as unknown as MockAccounting;
    await waitForUpgradeTx(upgraded);

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

  it("treats the signer/module split as a fresh pre-mainnet layout", async function () {
    this.timeout(120000);

    const deployer = getDeployer();
    const siweAuthAddress = await mockSiweAuth.getAddress();
    const PreviousFactory = await ethers.getContractFactory('MockAccountingPrevious', deployer);
    const previous = await upgrades.deployProxy(
      PreviousFactory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: 'uups',
        initializer: 'initialize',
        constructorArgs: [siweAuthAddress],
        unsafeAllow: ['constructor', 'state-variable-immutable'],
      },
    );
    await previous.waitForDeployment();
    const previousProxyAddress = await previous.getAddress();

    const AccountingFactory = await ethers.getContractFactory('MockAccounting', deployer);
    try {
      await upgrades.upgradeProxy(previousProxyAddress, AccountingFactory, {
        kind: 'uups',
        constructorArgs: [siweAuthAddress],
        unsafeAllow: ['constructor', 'state-variable-immutable', 'delegatecall'],
      });
      expect.fail('Expected previous inline-signing layout upgrade to be rejected');
    } catch (e: any) {
      expect(e.message).to.match(/deleted|inserted|layout|upgrade safe/i);
    }
  });

  it("Should only allow owner to upgrade", async function () {
    const attacker = getDeployer(1);

    const AccountingV2Factory = await ethers.getContractFactory('MockAccounting', attacker);

    // TODO: https://github.com/oasisprotocol/sapphire-paratime/issues/688
    const network = await ethers.provider.getNetwork();
    if ((0x5afd <= network.chainId) && (network.chainId <= 0x5aff)) {
      const upgradeFactory = await upgrades.upgradeProxy(proxyAddress, AccountingV2Factory, {
        kind: 'uups',
        constructorArgs: [await mockSiweAuth.getAddress()],
        unsafeAllow: ['constructor', 'state-variable-immutable', 'delegatecall'],
      });

      let receipt = await ethers.provider.getTransactionReceipt(upgradeFactory.deployTransaction!.hash);
      while (!receipt) {
        await new Promise(resolve => setTimeout(resolve, 100));
        receipt = await ethers.provider.getTransactionReceipt(upgradeFactory.deployTransaction!.hash);
      }
      expect(receipt!.status).to.equal(0);
    } else {
      await expect(
        upgrades.upgradeProxy(proxyAddress, AccountingV2Factory, {
          kind: 'uups',
          constructorArgs: [await mockSiweAuth.getAddress()],
          unsafeAllow: ['constructor', 'state-variable-immutable', 'delegatecall'],
        })
      ).to.be.revertedWithCustomError(accounting, "OwnableUnauthorizedAccount");
    }
  });

  it("Should prevent re-initialization", async function () {
    const deployer = getDeployer();

    await expect(
      accounting.initialize(MOCK_ROFL_APP_ID, deployer.address)
    ).to.be.reverted; // WithCustomError(accounting, "InvalidInitialization"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

  it("Should prevent initialization on implementation contract directly", async function () {
    const deployer = getDeployer();

    // Deploy implementation directly (not via proxy)
    const AccountingFactory = await ethers.getContractFactory('MockAccounting');
    const implementation = await AccountingFactory.deploy(await mockSiweAuth.getAddress());
    await implementation.waitForDeployment();

    // _disableInitializers() in the constructor should block initialize()
    await expect(
      implementation.initialize(MOCK_ROFL_APP_ID, deployer.address)
    ).to.be.reverted; // WithCustomError(implementation, "InvalidInitialization"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });

  it("Should reject upgrade to non-UUPS contract", async function () {
    // MockSiweAuth is a plain (non-UUPS) contract — upgrading to it should fail
    const NonUUPSFactory = await ethers.getContractFactory('MockSiweAuth');

    // OZ plugin validates upgrade safety off-chain before sending any tx
    try {
      await upgrades.upgradeProxy(proxyAddress, NonUUPSFactory, {
        kind: 'uups',
        constructorArgs: ["test-domain"],
      });
      expect.fail("Expected upgrade to non-UUPS contract to be rejected");
    } catch (e: any) {
      expect(e.message).to.include("not upgrade safe");
    }
  });

  it("Should support V2 upgrade with new state variables and reinitializer", async function () {
    const tokenInfoBefore = await accounting.tokens(TEST_TOKEN.tokenId);

    // Upgrade to V2 (reinitializer doesn't chain parent inits — they ran in V1)
    const AccountingV2Factory = await ethers.getContractFactory('MockAccountingV2');
    const upgraded = await upgrades.upgradeProxy(proxyAddress, AccountingV2Factory, {
      kind: 'uups',
      unsafeAllow: ['missing-initializer', 'constructor', 'state-variable-immutable', 'delegatecall'],
      constructorArgs: [await mockSiweAuth.getAddress()],
    }) as unknown as MockAccountingV2;
    await waitForUpgradeTx(upgraded);

    // Call reinitializer
    await upgraded.initializeV2(42);

    // Verify new state is set
    expect(await upgraded.newStateVar()).to.equal(42);

    // Verify existing state is preserved
    const tokenInfoAfter = await upgraded.tokens(TEST_TOKEN.tokenId);
    expect(tokenInfoAfter.tokenType).to.equal(tokenInfoBefore.tokenType, "Token info should survive V2 upgrade");
    expect(tokenInfoAfter.data).to.equal(tokenInfoBefore.data, "Token data should survive V2 upgrade");

    // Reinitializer should not be callable again
    await expect(
      upgraded.initializeV2(99)
    ).to.be.reverted; // WithCustomError(upgraded, "InvalidInitialization"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
  });
});
