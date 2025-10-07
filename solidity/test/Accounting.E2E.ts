import { expect, version } from 'chai';
import { ethers } from 'hardhat';
import { parseEther, Wallet } from 'ethers';
import { Accounting } from '../typechain-types';
import { generateERC20Tx } from './utils';
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
  // keccak256(abi.encodePacked(uint256(31337), address(0x0000000000000000000000000000000000000001)))
  // Precomputed to save time and avoid dependency on ethers.utils.solidityPack
  // which is not available in the ethers v6 version used by hardhat
  tokenId: "0x2caec014e240ce3c23bc5030e72c3cef9d88d6060dccc6f870e33c3a58a42132",
  chainId: 31337,
  address: '0x0000000000000000000000000000000000000001',
};

const userWallet1 = Wallet.createRandom().connect(ethers.provider);
const userWallet2 = Wallet.createRandom().connect(ethers.provider);

describe('Accounting', function () {
  let accounting: Accounting;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };

  before(async () => {
    const provider = ethers.provider;

    const AccountingFactory = await ethers.getContractFactory('Accounting');
    accounting = await AccountingFactory.deploy();
    await accounting.waitForDeployment();

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

    expect(tokenId).to.equal(TEST_TOKEN.tokenId);
    expect(await accounting.decodeEVMErc20TokenData(data)).to.deep.equal([TEST_TOKEN.chainId, TEST_TOKEN.address]);
  });

  it("User should be able to deposit", async function () {
    const depositAddress = await accounting.evmAddress();

    const tx = await generateERC20Tx({
      signer: userWallet1,
      tokenAddress: TEST_TOKEN.address,
      to: depositAddress,
      amount: parseEther("10"),
      chainId: TEST_TOKEN.chainId,
      nonce: 1,
      type: 0
    });

    // Check balance before
    const balanceBefore = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

    // Submit the deposit to Accounting contract
    await accounting.includeEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, tx, { rlpBlockHeader: "0x", transactionIndexRlp: "0x", transactionProofStack: "0x" });

    const balanceAfter = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

    expect(balanceBefore).to.equal(0);
    expect(balanceAfter).to.equal(parseEther("10"));
  });

  it("Test EIP712 transfer", async function () {
    const signature = await userWallet1.signTypedData(
      domain,
      { Transfer: types.Transfer },
      {
        userAddress: userWallet1.address,
        toAddress: userWallet2.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseEther("1"),
      }
    );

    // Check balances before
    const balance1Before = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2Before = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.transferFunds(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseEther("1"),
      signature
    );
    await tx.wait();

    const balance1After = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2After = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

    expect(balance1Before).to.equal(parseEther("10"));
    expect(balance1After).to.equal(parseEther("9"));
    expect(balance2Before).to.equal(0);
    expect(balance2After).to.equal(parseEther("1"));
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
        amount: parseEther("1"),
        expiry
      }
    );

    // Check balances before
    const balance1Before = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2Before = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.lockFunds(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseEther("1"),
      expiry,
      signature
    );
    await tx.wait();

    const balance1After = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);
    const balance2After = await accounting.balances(userWallet2.address, TEST_TOKEN.tokenId);

    expect(balance1Before).to.equal(parseEther("9"));
    expect(balance1After).to.equal(parseEther("8"));
    expect(balance2Before).to.equal(parseEther("1"));
    expect(balance2After).to.equal(parseEther("1"));

    // It doesn't go to the normal balance, instead a lock is appended to the user info
    const userLocks = await accounting.getUserLocks(userWallet1.address);

    expect(userLocks.length).to.equal(1);
    expect(userLocks[0][0]).to.equal(userWallet2.address);
    expect(userLocks[0][1]).to.equal(TEST_TOKEN.tokenId);
    expect(userLocks[0][2]).to.equal(parseEther("1"));
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
        amount: parseEther("0.5"),
      }
    );

    // Check balances before
    const balance1Before = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

    // Submit the transfer to Accounting contract
    const tx = await accounting.transferLockedFunds(
      userWallet1.address,
      userWallet2.address,
      0,
      parseEther("0.5"),
      signature
    );
    await tx.wait();


  });

  it("The user should be able to unlock the remaining locked funds after expiry", async function () {
    // Fast forward time by 2 hours
    await ethers.provider.send("evm_increaseTime", [2 * 3600]);
    await ethers.provider.send("evm_mine", []);

    await accounting.unlockFunds(userWallet1.address, 0);

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
          amount: parseEther("0.1"),
          expiry: expiry + i
        }
      );

      // Submit the transfer to Accounting contract
      const tx = await accounting.lockFunds(
        userWallet1.address,
        userWallet2.address,
        TEST_TOKEN.tokenId,
        parseEther("0.1"),
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
        amount: parseEther("0.1"),
        expiry: expiry + 11
      }
    );

    await expect(accounting.lockFunds(
      userWallet1.address,
      userWallet2.address,
      TEST_TOKEN.tokenId,
      parseEther("0.1"),
      expiry + 11,
      signature
    )).to.be.revertedWith("Too many active locks");
  });


  it("User should be able to withdraw TEST token using EIP712 signature", async function () {
    const signature = await userWallet1.signTypedData(
      domain,
      { Withdraw: types.Withdraw },
      {
        userAddress: userWallet1.address,
        tokenId: TEST_TOKEN.tokenId,
        amount: parseEther("0.1"),
      }
    );

    // Submit the transfer to Accounting contract
    const tx = await accounting.withdrawFunds(
      userWallet1.address,
      TEST_TOKEN.tokenId,
      parseEther("0.1"),
      signature
    );
    await tx.wait();
  });

});
