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
      ).to.be.revertedWithCustomError(mockEVMSignerAndVerifier, "InvalidGasPrice");
    });
  });
});
