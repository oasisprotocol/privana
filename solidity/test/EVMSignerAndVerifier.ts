import { expect, version } from 'chai';
import { ethers } from 'hardhat';
import { parseEther, Wallet } from 'ethers';
import { MockEVMSignerAndVerifier } from '../typechain-types';
import { generateERC20Tx, generateNativeTx } from './utils';
import { decode } from 'rlp';

const TEST_TOKEN = {
  tokenType: 1, // ERC20
  // keccak256(abi.encode(info.tokenType, info.data));
  // Precomputed to save time and avoid dependency on ethers.utils.solidityPack
  // which is not available in the ethers v6 version used by hardhat
  tokenId: "0x79f44edab45961a5df521cfbebc927dec4b0e45ef70411d65543ce55add20fff",
  chainId: 31337,
  address: '0x9dE8386e0082d83c235c4Dc1Eb287dED5ed29d35',
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
  let mockEVMSignerAndVerifier: MockEVMSignerAndVerifier;

  before(async () => {
    const provider = ethers.provider;

    // Any eth passed to constructor will be sent to the random wallet
    const MockEVMSignerAndVerifierFactory = await ethers.getContractFactory('MockEVMSignerAndVerifier');
    mockEVMSignerAndVerifier = await MockEVMSignerAndVerifierFactory.deploy();
    await mockEVMSignerAndVerifier.waitForDeployment();
  });

  it("User should be able to deposit TEST token using every transaction type", async function () {
    const depositAddress = await mockEVMSignerAndVerifier.evmAddress();

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

      const decodedTransaction = (await mockEVMSignerAndVerifier.exposedDecodeEVMTransaction.staticCall(tx));

      /*
            uint256 chainId,
            bytes32 hash,
            address from,
            address to,
            uint256 value,
            bytes memory txData,
            uint256 v,
            uint256 r,
            uint256 s
      */
      expect(decodedTransaction[0]).to.equal(TEST_TOKEN.chainId);
      expect(decodedTransaction[2]).to.equal(userWallet1.address);
      expect(decodedTransaction[3]).to.equal(TEST_TOKEN.address);
      expect(decodedTransaction[4]).to.equal(0); // value

      const decodedTxData = await mockEVMSignerAndVerifier.exposedDecodeTxDataForErc20Transfer.staticCall(decodedTransaction[5]);

      expect(decodedTxData[0]).to.equal(depositAddress);
      expect(decodedTxData[1]).to.equal(parseEther("10"));
    }

  });

  it("User should be able to deposit NATIVE token using every transaction type", async function () {
    const depositAddress = await mockEVMSignerAndVerifier.evmAddress();

    for (let type = 0; type <= 2; type++) {
      const tx = await generateNativeTx({
        signer: userWallet1,
        to: depositAddress,
        amount: parseEther("10"),
        chainId: NATIVE_TOKEN.chainId,
        nonce: 1,
        type
      });

      const decodedTransaction = (await mockEVMSignerAndVerifier.exposedDecodeEVMTransaction.staticCall(tx));

      /*
            uint256 chainId,
            bytes32 hash,
            address from,
            address to,
            uint256 value,
            bytes memory txData,
            uint256 v,
            uint256 r,
            uint256 s
      */
      expect(decodedTransaction[0]).to.equal(TEST_TOKEN.chainId);
      expect(decodedTransaction[2]).to.equal(userWallet1.address);
      expect(decodedTransaction[3]).to.equal(depositAddress);
      expect(decodedTransaction[4]).to.equal(parseEther("10")); // value
      expect(decodedTransaction[5]).to.equal("0x"); // txData
    }
  });
});
