import { expect } from 'chai';
import { ethers } from 'hardhat';
import { getDeployer } from './utils';
import { MockEVMSignerAndVerifier } from '../typechain-types';

describe('EVMSignerAndVerifier', function () {
  let mockEVMSignerAndVerifier: MockEVMSignerAndVerifier;

  const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21); // bytes21

  before(async () => {
    const deployer = getDeployer();
    const MockEVMSignerAndVerifierFactory = await ethers.getContractFactory('MockEVMSignerAndVerifier', deployer);
    mockEVMSignerAndVerifier = await MockEVMSignerAndVerifierFactory.deploy() as unknown as MockEVMSignerAndVerifier;
    await mockEVMSignerAndVerifier.waitForDeployment();
    await (await mockEVMSignerAndVerifier.initialize(MOCK_ROFL_APP_ID)).wait();

    // Re-attach via the default (wrapped) signer/provider: getDeployer()'s unwrapped
    // provider is only needed for deployment. On Sapphire networks, hardhat-chai-matchers'
    // .to.emit()/.to.be.reverted fetch the receipt via contract.runner.provider right after
    // sending — the unwrapped provider doesn't wait for inclusion, so that lookup races the
    // block. Mirrors the same workaround in utils.ts's deployMockAccounting.
    mockEVMSignerAndVerifier = (await ethers.getContractFactory('MockEVMSignerAndVerifier'))
      .attach(await mockEVMSignerAndVerifier.getAddress()) as unknown as MockEVMSignerAndVerifier;
  });

  it("Should have initialized evmAddress correctly", async function () {
    const evmAddress = await mockEVMSignerAndVerifier.evmAddress();
    // TEST_ADDRESS from MockEVMSignerAndVerifier contract
    expect(evmAddress).to.equal("0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2");
  });

  describe("setGasPrice", function () {
    it("should set the gas price and emit GasPriceSet", async function () {
      const chainId = 84532;
      const gasPrice = 1000000000n; // 1 gwei

      await expect(mockEVMSignerAndVerifier.setGasPrice(chainId, gasPrice))
        .to.emit(mockEVMSignerAndVerifier, "GasPriceSet")
        .withArgs(chainId, gasPrice);

      expect(await mockEVMSignerAndVerifier.gasPrices(chainId)).to.equal(gasPrice);
    });

    it("should reject a zero gas price", async function () {
      await expect(
        mockEVMSignerAndVerifier.setGasPrice(84532, 0n)
      ).to.be.reverted; // WithCustomError(mockEVMSignerAndVerifier, "InvalidGasPrice"); // https://github.com/oasisprotocol/sapphire-paratime/issues/688
    });
  });

  describe("gas limits and transaction generation", function () {
    it("should report gasLimitNativeSweep as 25000", async function () {
      expect(await mockEVMSignerAndVerifier.gasLimitNativeSweep()).to.equal(25000n);
    });

    it("should encode gasLimit 25000 in generated native sweep transaction", async function () {
      const network = await ethers.provider.getNetwork();
      if (network.chainId < 0x5afd || network.chainId > 0x5aff) {
        this.skip();
      }

      const signer = (await ethers.getSigners())[0];
      await (await mockEVMSignerAndVerifier.setRoflSignerAddress(signer.address)).wait();

      const beneficiary = getDeployer(1).address;
      const chainId = 23295n;
      const amount = ethers.parseEther("1.0");
      const gasPrice = 100000000000n; // 100 gwei
      const sourceChainNonce = 0n;

      const contractAddress = await mockEVMSignerAndVerifier.getAddress();
      const mnemonic = 'chimney theory present latin find behave ankle clock shadow earn suit reflect';

      // generateSweepNativeTransfer requires onlyROFLQuery: plain eth_call cannot authenticate
      // msg.sender, so this requires a Sapphire signed query (via sapphirepy).
      const script = `
import asyncio
from web3 import AsyncWeb3, AsyncHTTPProvider, Web3
from eth_account import Account
from sapphirepy import sapphire

Account.enable_unaudited_hdwallet_features()
acct = Account.from_mnemonic('${mnemonic}', account_path="m/44'/60'/0'/0/0")
w3 = AsyncWeb3(AsyncHTTPProvider('http://localhost:8545'))
wrapped = sapphire.wrap(w3, acct)
wrapped.eth.default_account = acct.address

abi = [{
    'inputs': [
        {'name': 'beneficiary', 'type': 'address'},
        {'name': 'chainType', 'type': 'uint8'},
        {'name': 'version', 'type': 'uint256'},
        {'name': 'chainId', 'type': 'uint256'},
        {'name': 'amount', 'type': 'uint256'},
        {'name': 'sourceChainNonce', 'type': 'uint64'},
        {'name': 'gasPrice', 'type': 'uint256'}
    ],
    'name': 'generateSweepNativeTransfer',
    'outputs': [{'name': 'signedTx', 'type': 'bytes'}],
    'stateMutability': 'view',
    'type': 'function'
}]

contract = wrapped.eth.contract(address=Web3.to_checksum_address('${contractAddress}'), abi=abi)

async def run():
    res = await contract.functions.generateSweepNativeTransfer(
        Web3.to_checksum_address('${beneficiary}'),
        0,
        0,
        ${chainId},
        ${amount},
        ${sourceChainNonce},
        ${gasPrice}
    ).call()
    print(res.hex())

asyncio.run(run())
`;
      const { execSync } = await import('child_process');
      const out = execSync(`uv run --active python -c "${script.replace(/"/g, '\\"')}"`, { cwd: '..' }).toString().trim();
      const signedTx = '0x' + out;

      const parsedTx = ethers.Transaction.from(signedTx);
      expect(parsedTx.gasLimit).to.equal(25000n);
      expect(parsedTx.chainId).to.equal(chainId);
      expect(parsedTx.value).to.equal(amount);
      expect(parsedTx.gasPrice).to.equal(gasPrice);
      expect(parsedTx.to).to.equal(await mockEVMSignerAndVerifier.evmAddress());
    });

    it("should encode gasLimit 25000 in generated gas funding transaction", async function () {
      const network = await ethers.provider.getNetwork();
      if (network.chainId < 0x5afd || network.chainId > 0x5aff) {
        this.skip();
      }

      const signer = (await ethers.getSigners())[0];
      await (await mockEVMSignerAndVerifier.setRoflSignerAddress(signer.address)).wait();

      const toDepositAddress = getDeployer(1).address;
      const chainId = 23295n;
      const gasAmount = 6500000000000000n; // 65000 * 100 gwei
      const gasTankNonce = 0n;
      const gasPrice = 100000000000n; // 100 gwei

      const contractAddress = await mockEVMSignerAndVerifier.getAddress();
      const mnemonic = 'chimney theory present latin find behave ankle clock shadow earn suit reflect';

      const script = `
import asyncio
from web3 import AsyncWeb3, AsyncHTTPProvider, Web3
from eth_account import Account
from sapphirepy import sapphire

Account.enable_unaudited_hdwallet_features()
acct = Account.from_mnemonic('${mnemonic}', account_path="m/44'/60'/0'/0/0")
w3 = AsyncWeb3(AsyncHTTPProvider('http://localhost:8545'))
wrapped = sapphire.wrap(w3, acct)
wrapped.eth.default_account = acct.address

abi = [{
    'inputs': [
        {'name': 'toDepositAddress', 'type': 'address'},
        {'name': 'chainId', 'type': 'uint256'},
        {'name': 'gasAmount', 'type': 'uint256'},
        {'name': 'gasTankNonce', 'type': 'uint64'},
        {'name': 'gasPrice', 'type': 'uint256'}
    ],
    'name': 'generateGasFundingTx',
    'outputs': [{'name': 'signedTx', 'type': 'bytes'}],
    'stateMutability': 'view',
    'type': 'function'
}]

contract = wrapped.eth.contract(address=Web3.to_checksum_address('${contractAddress}'), abi=abi)

async def run():
    res = await contract.functions.generateGasFundingTx(
        Web3.to_checksum_address('${toDepositAddress}'),
        ${chainId},
        ${gasAmount},
        ${gasTankNonce},
        ${gasPrice}
    ).call()
    print(res.hex())

asyncio.run(run())
`;
      const { execSync } = await import('child_process');
      const out = execSync(`uv run --active python -c "${script.replace(/"/g, '\\"')}"`, { cwd: '..' }).toString().trim();
      const signedTx = '0x' + out;

      const parsedTx = ethers.Transaction.from(signedTx);
      expect(parsedTx.gasLimit).to.equal(25000n);
      expect(parsedTx.chainId).to.equal(chainId);
      expect(parsedTx.value).to.equal(gasAmount);
      expect(parsedTx.gasPrice).to.equal(gasPrice);
      expect(parsedTx.to).to.equal(toDepositAddress);
    });
  });
});
