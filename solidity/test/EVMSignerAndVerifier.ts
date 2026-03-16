import { expect } from 'chai';
import { ethers, upgrades } from 'hardhat';
import { getDeployer } from './utils';
import { MockEVMSignerAndVerifier } from '../typechain-types';

describe('EVMSignerAndVerifier', function () {
  let mockEVMSignerAndVerifier: MockEVMSignerAndVerifier;

  const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21); // bytes21

  before(async () => {
    const deployer = getDeployer();
    const MockEVMSignerAndVerifierFactory = await ethers.getContractFactory('MockEVMSignerAndVerifier', deployer);
    // Deploy as UUPS proxy to properly initialize the contract
    mockEVMSignerAndVerifier = await upgrades.deployProxy(
      MockEVMSignerAndVerifierFactory,
      [MOCK_ROFL_APP_ID],
      { kind: 'uups', initializer: 'initialize' }
    ) as unknown as MockEVMSignerAndVerifier;
    await mockEVMSignerAndVerifier.waitForDeployment();
  });

  it("Should have initialized evmAddress correctly", async function () {
    const evmAddress = await mockEVMSignerAndVerifier.evmAddress();
    // TEST_ADDRESS from MockEVMSignerAndVerifier contract
    expect(evmAddress).to.equal("0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2");
  });
});
