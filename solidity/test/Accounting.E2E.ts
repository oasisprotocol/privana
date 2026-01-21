import { expect, version } from 'chai';
import { ethers, config } from 'hardhat';
import { keccak256, parseEther, Wallet } from 'ethers';
import { Accounting, MockShoyuBashi } from '../typechain-types';
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
  TransferLocked: [
    { name: "userAddress", type: "address" },
    { name: "toAddress", type: "address" },
    { name: "lockIndex", type: "uint256" },
    { name: "amount", type: "uint256" },
  ],
  Transfer: [
    { name: "userAddress", type: "address" },
    { name: "toAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
  ],
  Withdraw: [
    { name: "userAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
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

describe('Accounting', function () {
  let accounting: Accounting;
  let mockShoyubashi: MockShoyuBashi;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };
  let userWallet1: Wallet;
  let userWallet2: Wallet;

  before(async () => {
    const provider = ethers.provider;

    const MockShoyubashiFactory = await ethers.getContractFactory('MockShoyuBashi');
    mockShoyubashi = await MockShoyubashiFactory.deploy();
    await mockShoyubashi.waitForDeployment();

    const AccountingFactory = await ethers.getContractFactory('Accounting');
    accounting = await AccountingFactory.deploy(await mockShoyubashi.getAddress());
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
    const depositAddress = await accounting.evmAddress();

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

  it("Should reject deposit with invalid proof", async function () {
    const depositAddress = await accounting.evmAddress();
    const provider = new ethers.JsonRpcProvider("https://sepolia.base.org");

    const blockNumber = 32680090;
    const transactionIndex = 45;

    const { rlpBlockHeader, proof } = await getTxInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    const { proof: receiptProof } = await getReceiptInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    await mockShoyubashi.setUnanimousHash(TEST_TOKEN.chainId, blockNumber, keccak256(rlpBlockHeader));

    const invalidProof = proof.slice(0, proof.length - 1);

    await expect(
      accounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
        rlpBlockHeader,
        transactionIndexRlp: getRlpUint(transactionIndex),
        transactionProofStack: ethers.encodeRlp(invalidProof.map((rlpList) => ethers.decodeRlp(rlpList))),
      },
        {
          receiptIndexRlp: getRlpUint(transactionIndex),
          receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        }
      )
    ).to.be.reverted;
  });


  it("Should reject deposit with invalid receipt proof", async function () {
    const depositAddress = await accounting.evmAddress();
    const provider = new ethers.JsonRpcProvider("https://sepolia.base.org");

    const blockNumber = 32680090;
    const transactionIndex = 45;

    const { rlpBlockHeader, proof } = await getTxInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    const { proof: receiptProof } = await getReceiptInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    await mockShoyubashi.setUnanimousHash(TEST_TOKEN.chainId, blockNumber, keccak256(rlpBlockHeader));

    const invalidProof = receiptProof.slice(0, proof.length - 1);

    await expect(
      accounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
        rlpBlockHeader,
        transactionIndexRlp: getRlpUint(transactionIndex),
        transactionProofStack: ethers.encodeRlp(proof.map((rlpList) => ethers.decodeRlp(rlpList))),
      },
        {
          receiptIndexRlp: getRlpUint(transactionIndex),
          receiptProofStack: ethers.encodeRlp(invalidProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        }
      )
    ).to.be.reverted;
  });

  it("Should reject deposit with wrong block hash", async function () {
    const depositAddress = await accounting.evmAddress();
    const provider = new ethers.JsonRpcProvider("https://sepolia.base.org");

    const blockNumber = 32680090;
    const transactionIndex = 45;

    const { rlpBlockHeader, proof } = await getTxInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    const { proof: receiptProof } = await getReceiptInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    const wrongBlockHash = "0x1234567890123456789012345678901234567890123456789012345678901234";
    await mockShoyubashi.setUnanimousHash(TEST_TOKEN.chainId, blockNumber, wrongBlockHash);

    await expect(
      accounting.creditEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, {
        rlpBlockHeader,
        transactionIndexRlp: getRlpUint(transactionIndex),
        transactionProofStack: ethers.encodeRlp(proof.map((rlpList) => ethers.decodeRlp(rlpList))),
      },
        {
          receiptIndexRlp: getRlpUint(transactionIndex),
          receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList))),
        })
    ).to.be.revertedWith("Invalid block hash");
  });

  it("Test EIP712 transfer", async function () {
    const signature = await userWallet1.signTypedData(
      domain,
      { Transfer: types.Transfer },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("1"),
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
    const expiry = Math.floor(Date.now() / 1000) + 3600; // 1 hour from now

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
    expect(userLocks[0][0]).to.equal(userWallet2.address);
    expect(userLocks[0][1]).to.equal(TEST_TOKEN.tokenId);
    expect(userLocks[0][2]).to.equal(parseUsdt("1"));
    expect(userLocks[0][3]).to.be.equal(expiry);
  });

  it("The service should be able to resolve the lock", async function () {
    const signature = await userWallet2.signTypedData(
      domain,
      { TransferLocked: types.TransferLocked },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        lockIndex: 0,
        amount: parseUsdt("0.5"),
      }
    );

    // Check balances before
    const balance1Before = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.transferFromLock(
      userWallet1.address,
      userWallet2.address,
      0,
      parseUsdt("0.5"),
      signature
    );
    await tx.wait();


  });

  it("The user should be able to unlock the remaining locked funds after expiry", async function () {
    // Fast forward time by 2 hours
    await ethers.provider.send("evm_increaseTime", [2 * 3600]);
    await ethers.provider.send("evm_mine", []);

    await accounting.unlockSingleLock(userWallet1.address, 0);

    const userLocks = await accounting.getUserLocks(userWallet1.address);
    expect(userLocks.length).to.equal(0);

  });

  it("The user shouldn't be able to create more than 10 locks", async function () {
    const expiry = Math.floor(Date.now() / 1000) + 3600; // 1 hour from now

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
    const signature = await userWallet1.signTypedData(
      domain,
      { Withdraw: types.Withdraw },
      {
        userAddress: userWallet1.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseUsdt("0.1"),
      }
    );

    // Submit the transfer to Accounting contract
    const tx = await accounting.requestWithdrawal(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseUsdt("0.1"),
      signature
    );
    await tx.wait();

    const withdrawals = await accounting.withdrawals(0);
    expect(withdrawals.userAddress).to.equal(userWallet1.address);
    expect(withdrawals.amount).to.equal(parseUsdt("0.1"));
    expect(withdrawals.tokenId).to.equal(TEST_TOKEN.tokenId);
    expect(withdrawals.resolved).to.equal(false);

    // // Wait 1 block by waiting 20 seconds
    // await new Promise(resolve => setTimeout(resolve, 20000));

    // Admin resolves the withdrawal
    const tx2 = await accounting.resolveWithdrawal(0);
    const receipt2 = await tx2.wait();

  });

});
