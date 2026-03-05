import { ethers } from 'hardhat';
import { ProvethVerifier } from '../typechain-types';
import { getReceiptInclusionProof, getRlpUint, getTxInclusionProof } from './utils';

const RPC_URL = process.env.BASE_SEPOLIA_RPC_URL || "";


describe('ProvethVerifier', function () {
  let provethVerifier: ProvethVerifier;

  before(async function () {
    if (!RPC_URL) {
      console.log("BASE_SEPOLIA_RPC_URL not set, skipping ProvethVerifier tests");
      this.skip();
    }
    // Any eth passed to constructor will be sent to the random wallet
    const ProvethVerifierFactory = await ethers.getContractFactory('ProvethVerifier');
    provethVerifier = await ProvethVerifierFactory.deploy();
    await provethVerifier.waitForDeployment();
  });

  it("Should validate tx proofs correctly", async function () {
    const provider = new ethers.JsonRpcProvider(RPC_URL);

    const blockNumber = 32680090;
    const transactionIndex = 45;

    const { rlpBlockHeader, proof: txProof } = await getTxInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    const rawTx = await provethVerifier.validateTxProof({
      rlpBlockHeader,
      transactionIndexRlp: getRlpUint(transactionIndex),
      transactionProofStack: ethers.encodeRlp(txProof.map((rlpList) => ethers.decodeRlp(rlpList)))
    });

    console.log("Raw transaction:", rawTx);
  });

  it("Should verify tx receipt proofs correctly", async function () {
    const provider = new ethers.JsonRpcProvider(RPC_URL);

    const blockNumber = 23488501;
    const transactionIndex = 1;

    const { rlpBlockHeader, proof: receiptProof } = await getReceiptInclusionProof(
      provider,
      blockNumber,
      transactionIndex
    );

    const rawReceipt = await provethVerifier.validateReceiptProof(rlpBlockHeader, {
      receiptIndexRlp: getRlpUint(transactionIndex),
      receiptProofStack: ethers.encodeRlp(receiptProof.map((rlpList) => ethers.decodeRlp(rlpList)))
    });

    console.log("Raw receipt:", rawReceipt);
  });

});
