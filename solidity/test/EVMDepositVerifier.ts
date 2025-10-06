import { expect, version } from 'chai';
import { ethers } from 'hardhat';
import { parseEther, Wallet } from 'ethers';
import { Accounting } from '../typechain-types';
import { generateERC20Tx, generateNativeTx } from './utils';
// import {
//   isCalldataEnveloped,
//   wrapEthereumProvider,
// } from '@oasisprotocol/sapphire-paratime';

const TEST_TOKEN = {
  tokenType: 1, // ERC20
  // keccak256(abi.encode(info.tokenType, info.data));
  // Precomputed to save time and avoid dependency on ethers.utils.solidityPack
  // which is not available in the ethers v6 version used by hardhat
  tokenId: "0x2caec014e240ce3c23bc5030e72c3cef9d88d6060dccc6f870e33c3a58a42132",
  chainId: 31337,
  address: '0x0000000000000000000000000000000000000001',
};

const NATIVE_TOKEN = {
  tokenType: 0, // Native
  // keccak256(abi.encode(info.tokenType, info.data));
  // Precomputed to save time and avoid dependency on ethers.utils.solidityPack
  tokenId: "0x707a478b2f37fb8e6a4d2d9df53deefd8587e43b3102697c1cc6b735f05bb0fa",
  chainId: 31337,
}

const userWallet1 = Wallet.createRandom().connect(ethers.provider);
const userWallet2 = Wallet.createRandom().connect(ethers.provider);

describe('EVMSignerAndVerifier', function () {
  let accounting: Accounting;

  before(async () => {
    const provider = ethers.provider;

    // Any eth passed to constructor will be sent to the random wallet
    const AccountingFactory = await ethers.getContractFactory('Accounting');
    accounting = await AccountingFactory.deploy();
    await accounting.waitForDeployment();
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

  it("Admin adds tokenInfo for Native token", async function () {
    const [admin] = await ethers.getSigners();

    // Pad chainId to 32 bytes, then concatenate
    const data = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(NATIVE_TOKEN.chainId), 32),
    ]);

    const tx = await accounting.connect(admin).setTokenInfo({
      tokenType: NATIVE_TOKEN.tokenType,
      data: data
    });
    await tx.wait();

    const tokenId = await accounting.getTokenId({
      tokenType: NATIVE_TOKEN.tokenType,
      data: data
    });


    const tokenInfo = await accounting.tokens(tokenId);

    expect(tokenId).to.equal(NATIVE_TOKEN.tokenId);
    expect(await accounting.decodeEVMNativeTokenData(data)).to.equal(NATIVE_TOKEN.chainId);
  });

  it("User should be able to deposit TEST token using every transaction type", async function () {
    const depositAddress = await accounting.evmAddress();

    for (let type = 0; type <= 2; type++) {
      const tx = await generateERC20Tx({
        signer: userWallet1,
        tokenAddress: TEST_TOKEN.address,
        to: depositAddress,
        amount: parseEther("10"),
        chainId: TEST_TOKEN.chainId,
        nonce: 1,
        type
      });

      // Check balance before
      const balanceBefore = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

      // Submit the deposit to Accounting contract
      await accounting.includeEVMDeposit(userWallet1.address, TEST_TOKEN.tokenId, tx, { rlpBlockHeader: "0x", transactionIndexRlp: "0x", transactionProofStack: "0x" });

      const balanceAfter = await accounting.balances(userWallet1.address, TEST_TOKEN.tokenId);

      expect(balanceAfter - balanceBefore).to.equal(parseEther("10"));
    }

  });

  // it("User should be able to deposit NATIVE token using every transaction type", async function () {
  //   const depositAddress = await accounting.evmAddress();

  //   for (let type = 0; type <= 2; type++) {
  //     const tx = await generateNativeTx({
  //       signer: userWallet1,
  //       to: depositAddress,
  //       amount: parseEther("10"),
  //       chainId: NATIVE_TOKEN.chainId,
  //       nonce: 1,
  //       type
  //     });

  //     // Check balance before
  //     const balanceBefore = await accounting.balances(userWallet1.address, NATIVE_TOKEN.tokenId);

  //     // Submit the deposit to Accounting contract
  //     await accounting.includeEVMDeposit(userWallet1.address, NATIVE_TOKEN.tokenId, tx, { rlpBlockHeader: "0x", transactionIndexRlp: "0x", transactionProofStack: "0x" });

  //     const balanceAfter = await accounting.balances(userWallet1.address, NATIVE_TOKEN.tokenId);

  //     expect(balanceAfter - balanceBefore).to.equal(parseEther("10"));

  //   }

  // });


});
