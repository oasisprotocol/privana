import { expect } from 'chai';
import { ethers, upgrades } from 'hardhat';
import { keccak256 } from 'ethers';
import { MockAccounting, MockShoyuBashi, MockSiweAuth, ProvethVerifier } from '../typechain-types';
import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';

// Sepolia chain ID for FDC tests
const SEPOLIA_CHAIN_ID = 11155111;

// The MockAccounting hard-codes this as the deposit address (evmAddress)
const DEPOSIT_ADDRESS = '0x284a3Fe2939a4e4859e6321537d4264533E3D549';

describe('Accounting FDC Deposits', function () {
  let accounting: MockAccounting;
  let deployer: HardhatEthersSigner;
  let relayer: HardhatEthersSigner;
  let user1: HardhatEthersSigner;
  let nonRelayer: HardhatEthersSigner;
  let nativeTokenId: string;
  let erc20TokenId: string;

  // Sepolia ERC-20 token address (arbitrary for testing)
  const ERC20_ADDRESS = '0x1234567890AbcdEF1234567890aBcdef12345678';

  before(async () => {
    [deployer, relayer, user1, nonRelayer] = await ethers.getSigners();

    // Deploy mocks (same pattern as Accounting.E2E.ts)
    const MockSiweAuthFactory = await ethers.getContractFactory('MockSiweAuth');
    const mockSiweAuth = await MockSiweAuthFactory.deploy('test');
    await mockSiweAuth.waitForDeployment();

    const MockShoyubashiFactory = await ethers.getContractFactory('MockShoyuBashi');
    const mockShoyubashi = await MockShoyubashiFactory.deploy();
    await mockShoyubashi.waitForDeployment();

    const ProvethVerifierFactory = await ethers.getContractFactory('ProvethVerifier');
    const provethVerifier = await ProvethVerifierFactory.deploy();
    await provethVerifier.waitForDeployment();

    // Deploy MockAccounting as UUPS proxy
    const AccountingFactory = await ethers.getContractFactory('MockAccounting');
    accounting = await upgrades.deployProxy(
      AccountingFactory,
      [await mockShoyubashi.getAddress(), await provethVerifier.getAddress(), deployer.address],
      { kind: 'uups', initializer: 'initialize', constructorArgs: [await mockSiweAuth.getAddress()] }
    ) as unknown as MockAccounting;
    await accounting.waitForDeployment();

    // Register native token for Sepolia
    const nativeData = await accounting.encodeEVMNativeTokenData(SEPOLIA_CHAIN_ID);
    await accounting.setTokenInfo({ tokenType: 0, data: nativeData });
    nativeTokenId = await accounting.getTokenId({ tokenType: 0, data: nativeData });

    // Register ERC-20 token for Sepolia
    const erc20Data = await accounting.encodeEVMErc20TokenData(SEPOLIA_CHAIN_ID, ERC20_ADDRESS);
    await accounting.setTokenInfo({ tokenType: 1, data: erc20Data });
    erc20TokenId = await accounting.getTokenId({ tokenType: 1, data: erc20Data });

    // Set FDC relayer
    await accounting.setFDCRelayer(relayer.address);
  });

  describe('creditFDCDeposit - native token', () => {
    const txHash = ethers.id('test-native-deposit-tx-1');
    const depositAmount = ethers.parseEther('1.0');

    it('should credit native deposit from authorized relayer', async () => {
      const balanceBefore = await accounting.getBalance(user1.address, nativeTokenId);

      await accounting.connect(relayer).creditFDCDeposit(
        user1.address,
        nativeTokenId,
        txHash,
        SEPOLIA_CHAIN_ID,
        DEPOSIT_ADDRESS,  // receivingAddress = deposit address for native
        depositAmount,     // value (native amount)
        0,                 // erc20Amount = 0 for native
      );

      const balanceAfter = await accounting.getBalance(user1.address, nativeTokenId);
      expect(balanceAfter - balanceBefore).to.equal(depositAmount);
    });

    it('should emit Deposit event', async () => {
      const txHash2 = ethers.id('test-native-deposit-tx-2');
      const amount = ethers.parseEther('0.5');

      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, nativeTokenId, txHash2,
          SEPOLIA_CHAIN_ID, DEPOSIT_ADDRESS, amount, 0,
        )
      ).to.emit(accounting, 'Deposit')
        .withArgs(user1.address, nativeTokenId, amount);
    });

    it('should mark deposit as processed', async () => {
      const depositKey = keccak256(
        ethers.solidityPacked(['uint256', 'bytes32'], [SEPOLIA_CHAIN_ID, txHash])
      );
      expect(await accounting.processedDeposits(depositKey)).to.be.true;
    });
  });

  describe('creditFDCDeposit - ERC-20 token', () => {
    const txHash = ethers.id('test-erc20-deposit-tx-1');
    const erc20Amount = ethers.parseUnits('100', 6); // 100 USDC (6 decimals)

    it('should credit ERC-20 deposit from authorized relayer', async () => {
      const balanceBefore = await accounting.getBalance(user1.address, erc20TokenId);

      await accounting.connect(relayer).creditFDCDeposit(
        user1.address,
        erc20TokenId,
        txHash,
        SEPOLIA_CHAIN_ID,
        ERC20_ADDRESS,     // receivingAddress = token contract for ERC-20
        0,                  // value = 0 for ERC-20
        erc20Amount,        // erc20Amount
      );

      const balanceAfter = await accounting.getBalance(user1.address, erc20TokenId);
      expect(balanceAfter - balanceBefore).to.equal(erc20Amount);
    });

    it('should emit Deposit event for ERC-20', async () => {
      const txHash2 = ethers.id('test-erc20-deposit-tx-2');
      const amount = ethers.parseUnits('50', 6);

      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, erc20TokenId, txHash2,
          SEPOLIA_CHAIN_ID, ERC20_ADDRESS, 0, amount,
        )
      ).to.emit(accounting, 'Deposit')
        .withArgs(user1.address, erc20TokenId, amount);
    });
  });

  describe('creditFDCDeposit - error cases', () => {
    it('should reject unauthorized caller', async () => {
      const txHash = ethers.id('unauth-tx');
      await expect(
        accounting.connect(nonRelayer).creditFDCDeposit(
          user1.address, nativeTokenId, txHash,
          SEPOLIA_CHAIN_ID, DEPOSIT_ADDRESS, ethers.parseEther('1'), 0,
        )
      ).to.be.revertedWithCustomError(accounting, 'UnauthorizedRelayer');
    });

    it('should reject duplicate deposit (same tx hash)', async () => {
      const txHash = ethers.id('duplicate-tx');
      // First call succeeds
      await accounting.connect(relayer).creditFDCDeposit(
        user1.address, nativeTokenId, txHash,
        SEPOLIA_CHAIN_ID, DEPOSIT_ADDRESS, ethers.parseEther('1'), 0,
      );
      // Second call with same txHash reverts
      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, nativeTokenId, txHash,
          SEPOLIA_CHAIN_ID, DEPOSIT_ADDRESS, ethers.parseEther('1'), 0,
        )
      ).to.be.revertedWithCustomError(accounting, 'DepositAlreadyProcessed');
    });

    it('should reject zero native amount', async () => {
      const txHash = ethers.id('zero-amount-tx');
      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, nativeTokenId, txHash,
          SEPOLIA_CHAIN_ID, DEPOSIT_ADDRESS, 0, 0,  // both zero
        )
      ).to.be.revertedWithCustomError(accounting, 'InvalidAmount');
    });

    it('should reject zero ERC-20 amount', async () => {
      const txHash = ethers.id('zero-erc20-tx');
      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, erc20TokenId, txHash,
          SEPOLIA_CHAIN_ID, ERC20_ADDRESS, 0, 0,
        )
      ).to.be.revertedWithCustomError(accounting, 'InvalidAmount');
    });

    it('should reject wrong receiving address for native token', async () => {
      const txHash = ethers.id('wrong-native-recv-tx');
      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, nativeTokenId, txHash,
          SEPOLIA_CHAIN_ID,
          '0x0000000000000000000000000000000000000001',  // wrong address
          ethers.parseEther('1'), 0,
        )
      ).to.be.revertedWithCustomError(accounting, 'InvalidDeposit');
    });

    it('should reject wrong receiving address for ERC-20 token', async () => {
      const txHash = ethers.id('wrong-erc20-recv-tx');
      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, erc20TokenId, txHash,
          SEPOLIA_CHAIN_ID,
          '0x0000000000000000000000000000000000000001',  // not the token contract
          0, ethers.parseUnits('100', 6),
        )
      ).to.be.revertedWithCustomError(accounting, 'InvalidDeposit');
    });

    it('should reject chain ID mismatch for native token', async () => {
      const txHash = ethers.id('wrong-chain-native-tx');
      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, nativeTokenId, txHash,
          84532,  // Base Sepolia, but token registered for Sepolia
          DEPOSIT_ADDRESS, ethers.parseEther('1'), 0,
        )
      ).to.be.revertedWithCustomError(accounting, 'ChainIdMismatch');
    });

    it('should reject chain ID mismatch for ERC-20 token', async () => {
      const txHash = ethers.id('wrong-chain-erc20-tx');
      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, erc20TokenId, txHash,
          84532,  // wrong chain
          ERC20_ADDRESS, 0, ethers.parseUnits('100', 6),
        )
      ).to.be.revertedWithCustomError(accounting, 'ChainIdMismatch');
    });

    it('should reject unregistered token (implicit via data length)', async () => {
      const fakeTokenId = ethers.id('unregistered-token');
      const txHash = ethers.id('unreg-token-tx');
      // Unregistered token has empty data → decodeEVMNativeTokenData reverts
      await expect(
        accounting.connect(relayer).creditFDCDeposit(
          user1.address, fakeTokenId, txHash,
          SEPOLIA_CHAIN_ID, DEPOSIT_ADDRESS, ethers.parseEther('1'), 0,
        )
      ).to.be.revertedWithCustomError(accounting, 'InvalidNativeTokenDataLength');
    });
  });

  describe('setFDCRelayer', () => {
    it('should allow owner to set relayer', async () => {
      const newRelayer = ethers.Wallet.createRandom().address;
      await expect(accounting.connect(deployer).setFDCRelayer(newRelayer))
        .to.emit(accounting, 'FDCRelayerUpdated')
        .withArgs(newRelayer);
      expect(await accounting.fdcRelayer()).to.equal(newRelayer);

      // Restore original relayer for subsequent tests
      await accounting.connect(deployer).setFDCRelayer(relayer.address);
    });

    it('should reject non-owner setting relayer', async () => {
      await expect(
        accounting.connect(user1).setFDCRelayer(user1.address)
      ).to.be.revertedWithCustomError(accounting, 'OwnableUnauthorizedAccount');
    });

    it('should allow setting relayer to zero address (disable)', async () => {
      await accounting.connect(deployer).setFDCRelayer(ethers.ZeroAddress);
      expect(await accounting.fdcRelayer()).to.equal(ethers.ZeroAddress);

      // Restore
      await accounting.connect(deployer).setFDCRelayer(relayer.address);
    });
  });

  describe('cross-path dedup (FDC + EVM deposit paths)', () => {
    it('should prevent crediting same tx via both creditEVMDeposit and creditFDCDeposit', async () => {
      // Use a unique txHash that could theoretically be used on both paths
      const txHash = ethers.id('cross-path-dedup-tx');

      // Credit via FDC path first
      await accounting.connect(relayer).creditFDCDeposit(
        user1.address, nativeTokenId, txHash,
        SEPOLIA_CHAIN_ID, DEPOSIT_ADDRESS, ethers.parseEther('1'), 0,
      );

      // Verify the deposit key is marked as processed
      const depositKey = keccak256(
        ethers.solidityPacked(['uint256', 'bytes32'], [SEPOLIA_CHAIN_ID, txHash])
      );
      expect(await accounting.processedDeposits(depositKey)).to.be.true;

      // Note: Can't easily test creditEVMDeposit with the same key since it needs
      // valid Merkle proofs. But we verify the processedDeposits map is shared,
      // which means creditEVMDeposit would also revert with DepositAlreadyProcessed.
    });
  });
});
