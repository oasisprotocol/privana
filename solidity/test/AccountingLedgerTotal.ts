import { expect } from 'chai';
import { ethers, upgrades } from 'hardhat';
import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import { MockAccounting, MockSiweAuth } from '../typechain-types';
import { getCombinedAccountingAt, getLinkedAccountingFactory, wireHistoryModule } from './util/links';
import { CombinedMockAccounting } from './utils';

// Mirrors of TokenType in contracts/Types.sol.
const TokenType = { NativeEVM: 0, ERC20: 1, BridgeAsset: 2 } as const;

// Pinned in docs/rose-bridge-phase0-updated-plan.md (Cross-Layer Contract rule 2).
// Must equal getTokenId(TokenInfo(BridgeAsset, encodeBridgeAssetTokenData("ROSE"))).
const ROSE_TOKEN_ID_LITERAL =
  '0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa';

describe('AccountingLedgerTotal — BridgeAsset enum + helpers', () => {
  let accounting: MockAccounting;

  const MOCK_ROFL_APP_ID = '0x' + '00'.repeat(21); // bytes21

  before(async () => {
    const [deployer] = await ethers.getSigners();

    const SiweFactory = await ethers.getContractFactory('MockSiweAuth');
    const siwe = (await SiweFactory.deploy('test')) as unknown as MockSiweAuth;
    await siwe.waitForDeployment();

    const AccountingFactory = await getLinkedAccountingFactory('MockAccounting');
    accounting = (await upgrades.deployProxy(
      AccountingFactory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: 'uups',
        initializer: 'initialize',
        constructorArgs: [await siwe.getAddress()],
        unsafeAllow: ['external-library-linking'],
      },
    )) as unknown as MockAccounting;
    await accounting.waitForDeployment();
  });

  it('encodeBridgeAssetTokenData("ROSE") returns the UTF-8 bytes of "ROSE"', async () => {
    const encoded = await accounting.encodeBridgeAssetTokenData('ROSE');
    expect(encoded).to.equal(ethers.hexlify(ethers.toUtf8Bytes('ROSE')));
  });

  it('round-trips encode -> decode for "ROSE"', async () => {
    const encoded = await accounting.encodeBridgeAssetTokenData('ROSE');
    expect(await accounting.decodeBridgeAssetTokenData(encoded)).to.equal('ROSE');
  });

  it('decodeBridgeAssetTokenData reverts on empty bytes', async () => {
    await expect(accounting.decodeBridgeAssetTokenData('0x'))
      .to.be.revertedWithCustomError(accounting, 'UnsupportedTokenType');
  });

  it('getTokenId for BridgeAsset/ROSE matches the pinned ROSE_TOKEN_ID literal', async () => {
    const data = await accounting.encodeBridgeAssetTokenData('ROSE');
    const tokenId = await accounting.getTokenId({
      tokenType: TokenType.BridgeAsset,
      data,
    });
    expect(tokenId).to.equal(ROSE_TOKEN_ID_LITERAL);
  });
});

describe('AccountingLedgerTotal — ROSE_TOKEN_ID constant + ledger storage', () => {
  // Step 01.02 contract additions: bytes32 public constant ROSE_TOKEN_ID,
  // mapping(bytes32 => uint256) private _ledgerTotal (read via ledgerTotalOf),
  // mapping(uint256 => address) public roflBridgeAddress, and __gap shrunk
  // from [41] → [39].
  //
  // Note on __gap size: a runtime test would require enabling `storageLayout`
  // in hardhat's outputSelection (config-level change, out of scope here).
  // The shrink is enforced at the source level by the [39] literal, and the
  // OpenZeppelin upgrades plugin validates layout compatibility on actual
  // upgrade attempts. Step 03/01 will further reduce __gap to [38].

  let accounting: MockAccounting;

  const MOCK_ROFL_APP_ID = '0x' + '00'.repeat(21); // bytes21
  const BASE_SEPOLIA_CHAIN_ID = 84532;

  before(async () => {
    const [deployer] = await ethers.getSigners();

    const SiweFactory = await ethers.getContractFactory('MockSiweAuth');
    const siwe = (await SiweFactory.deploy('test')) as unknown as MockSiweAuth;
    await siwe.waitForDeployment();

    const AccountingFactory = await getLinkedAccountingFactory('MockAccounting');
    accounting = (await upgrades.deployProxy(
      AccountingFactory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: 'uups',
        initializer: 'initialize',
        constructorArgs: [await siwe.getAddress()],
        unsafeAllow: ['external-library-linking'],
      },
    )) as unknown as MockAccounting;
    await accounting.waitForDeployment();
  });

  it('ROSE_TOKEN_ID() returns the pinned literal (Cross-Layer Contract rule 2)', async () => {
    expect(await accounting.ROSE_TOKEN_ID()).to.equal(ROSE_TOKEN_ID_LITERAL);
  });

  it('ROSE_TOKEN_ID() equals getTokenId(BridgeAsset, encode("ROSE"))', async () => {
    const data = await accounting.encodeBridgeAssetTokenData('ROSE');
    const derived = await accounting.getTokenId({
      tokenType: TokenType.BridgeAsset,
      data,
    });
    expect(await accounting.ROSE_TOKEN_ID()).to.equal(derived);
  });

  it('ledgerTotalOf(ROSE_TOKEN_ID) == 0 at deploy', async () => {
    expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID_LITERAL)).to.equal(0n);
  });

  it('ledgerTotalOf returns 0 for an arbitrary tokenId at deploy', async () => {
    const arbitraryTokenId = ethers.keccak256(ethers.toUtf8Bytes('arbitrary'));
    expect(await accounting.ledgerTotalOf(arbitraryTokenId)).to.equal(0n);
  });

  it('roflBridgeAddress(base-sepolia) == address(0) at deploy', async () => {
    expect(await accounting.roflBridgeAddress(BASE_SEPOLIA_CHAIN_ID)).to.equal(
      ethers.ZeroAddress,
    );
  });
});

describe('AccountingLedgerTotal — _increaseLedgerTotal wiring on creditDeposit', () => {
  // Step 01.03: every creditDeposit path calls _increaseLedgerTotal after the
  // balance write. The helper guards on ROSE_TOKEN_ID, so non-ROSE tokens leave
  // _ledgerTotal untouched. The decrement helper exists but is exercised in Task 02.

  let accounting: MockAccounting;
  let userAddr: string;
  let baseTokenId: string;

  const MOCK_ROFL_APP_ID = '0x' + '00'.repeat(21);
  const BASE_SEPOLIA_CHAIN_ID = 84532;

  before(async () => {
    const [deployer, user] = await ethers.getSigners();
    userAddr = user.address;

    const SiweFactory = await ethers.getContractFactory('MockSiweAuth');
    const siwe = (await SiweFactory.deploy('test')) as unknown as MockSiweAuth;
    await siwe.waitForDeployment();

    const AccountingFactory = await getLinkedAccountingFactory('MockAccounting');
    accounting = (await upgrades.deployProxy(
      AccountingFactory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: 'uups',
        initializer: 'initialize',
        constructorArgs: [await siwe.getAddress()],
        unsafeAllow: ['external-library-linking'],
      },
    )) as unknown as MockAccounting;
    await accounting.waitForDeployment();
    // Wire history so creditDeposit paths can append entries.
    await wireHistoryModule(accounting);

    // Register ROSE as a BridgeAsset token (canonical ROSE_TOKEN_ID).
    const roseData = await accounting.encodeBridgeAssetTokenData('ROSE');
    await accounting.setTokenInfo({
      tokenType: TokenType.BridgeAsset,
      data: roseData,
    });

    // Register base-sepolia native ETH as a NativeEVM token (non-ROSE control).
    const baseData = await accounting.encodeEVMNativeTokenData(BASE_SEPOLIA_CHAIN_ID);
    await accounting.setTokenInfo({
      tokenType: TokenType.NativeEVM,
      data: baseData,
    });
    baseTokenId = await accounting.getTokenId({
      tokenType: TokenType.NativeEVM,
      data: baseData,
    });
  });

  it('ROSE deposit credits balance and increments ledgerTotalOf(ROSE_TOKEN_ID)', async () => {
    const depositId = ethers.keccak256(ethers.toUtf8Bytes('rose-deposit-1'));
    await accounting.mockCreditDeposit(userAddr, ROSE_TOKEN_ID_LITERAL, 100n, depositId);
    expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID_LITERAL)).to.equal(100n);
    expect(await accounting.getBalance(userAddr, ROSE_TOKEN_ID_LITERAL)).to.equal(100n);
  });

  it('subsequent ROSE deposits accumulate into ledgerTotalOf', async () => {
    const depositId = ethers.keccak256(ethers.toUtf8Bytes('rose-deposit-2'));
    await accounting.mockCreditDeposit(userAddr, ROSE_TOKEN_ID_LITERAL, 50n, depositId);
    expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID_LITERAL)).to.equal(150n);
  });

  it('NativeEVM deposit credits balance but leaves ledgerTotalOf(non-ROSE) at 0', async () => {
    const depositId = ethers.keccak256(ethers.toUtf8Bytes('base-deposit-1'));
    await accounting.mockCreditDeposit(userAddr, baseTokenId, 1000n, depositId);
    expect(await accounting.ledgerTotalOf(baseTokenId)).to.equal(0n);
    expect(await accounting.getBalance(userAddr, baseTokenId)).to.equal(1000n);
  });

  it('NativeEVM deposit leaves ledgerTotalOf(ROSE_TOKEN_ID) untouched', async () => {
    expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID_LITERAL)).to.equal(150n);
  });
});

describe('AccountingLedgerTotal — BridgeAsset rejection in lock paths', () => {
  // createLock / modifyLock / withdrawFromLock must revert with
  // BridgeAssetNotSupported when the token's tokenType is BridgeAsset. The
  // _scheduleWithdrawal backstop is exercised transitively via withdrawFromLock.
  //
  // createLock and withdrawFromLock guard on caller-supplied identifiers before
  // EIP-712 verification, so empty-bytes signatures suffice for their reject
  // paths. modifyLock recovers the user from the signature first, so its test
  // signs a valid ModifyLock message to reach the guard.

  let accounting: CombinedMockAccounting;
  let userAddr: string;
  let userSigner: HardhatEthersSigner;
  let serviceAddr: string;
  let domain: { name: string; version: string; chainId: number; verifyingContract: string };

  const MOCK_ROFL_APP_ID = '0x' + '00'.repeat(21);
  const FORCED_LOCK_ID = 1n;
  const FAR_FUTURE_EXPIRY = 2_000_000_000n; // ≈ 2033
  // EIP-712 ModifyLock type. modifyLock recovers the user from the signature
  // before reaching the BridgeAsset guard, so a valid signature is required to
  // exercise the reject path (unlike createLock / withdrawFromLock, which guard
  // on caller-supplied identifiers first).
  const MODIFY_LOCK_TYPE = {
    ModifyLock: [
      { name: 'lockId', type: 'uint256' },
      { name: 'amount', type: 'uint256' },
      { name: 'newExpiry', type: 'uint256' },
      { name: 'nonce', type: 'uint256' },
    ],
  };

  before(async () => {
    const [deployer, user, service] = await ethers.getSigners();
    userAddr = user.address;
    userSigner = user;
    serviceAddr = service.address;

    const SiweFactory = await ethers.getContractFactory('MockSiweAuth');
    const siwe = (await SiweFactory.deploy('test')) as unknown as MockSiweAuth;
    await siwe.waitForDeployment();

    const AccountingFactory = await getLinkedAccountingFactory('MockAccounting');
    const proxy = (await upgrades.deployProxy(
      AccountingFactory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: 'uups',
        initializer: 'initialize',
        constructorArgs: [await siwe.getAddress()],
        unsafeAllow: ['external-library-linking'],
      },
    )) as unknown as MockAccounting;
    await proxy.waitForDeployment();

    // Register ROSE so tokens[ROSE_TOKEN_ID].tokenType == BridgeAsset.
    const roseData = await proxy.encodeBridgeAssetTokenData('ROSE');
    await proxy.setTokenInfo({
      tokenType: TokenType.BridgeAsset,
      data: roseData,
    });

    // Give the user a non-zero ROSE balance so balance / amount checks
    // wouldn't be the first revert. The BridgeAsset guard fires earlier.
    await proxy.setBalance(userAddr, ROSE_TOKEN_ID_LITERAL, 1000n);

    // Wire the delegated lock module so the lock selectors route through the
    // proxy fallback — otherwise they revert LockModuleNotSet before reaching
    // the BridgeAsset guard under test.
    const LockModuleFactory = await ethers.getContractFactory('LockModule');
    const lockModule = await LockModuleFactory.deploy();
    await lockModule.waitForDeployment();
    await proxy.setLockModule(await lockModule.getAddress());

    // Force a BridgeAsset lock for modifyLock / withdrawFromLock tests —
    // createLock cannot mint one anymore.
    await proxy.mockForceLock(
      userAddr,
      FORCED_LOCK_ID,
      serviceAddr,
      ROSE_TOKEN_ID_LITERAL,
      100n,
      FAR_FUTURE_EXPIRY,
    );

    // Combined-ABI handle so the lock selectors are callable at the proxy.
    accounting = (await getCombinedAccountingAt(
      await proxy.getAddress(),
      deployer,
      ['MockAccounting', 'LockModule'],
    )) as unknown as CombinedMockAccounting;

    const d = await proxy.eip712Domain();
    domain = {
      name: d[1],
      version: d[2],
      chainId: Number(d[3]),
      verifyingContract: d[4],
    };
  });

  it('createLock reverts with BridgeAssetNotSupported for ROSE_TOKEN_ID', async () => {
    await expect(
      accounting.createLock(
        serviceAddr,
        ROSE_TOKEN_ID_LITERAL,
        100n,
        FAR_FUTURE_EXPIRY,
        0n,
        '0x',
      ),
    ).to.be.revertedWithCustomError(accounting, 'BridgeAssetNotSupported');
  });

  it('modifyLock reverts with BridgeAssetNotSupported on a forced BridgeAsset lock', async () => {
    const nonce = await accounting.modifyLockNonces(userAddr);
    const signature = await userSigner.signTypedData(domain, MODIFY_LOCK_TYPE, {
      lockId: FORCED_LOCK_ID,
      amount: 50n,
      newExpiry: FAR_FUTURE_EXPIRY,
      nonce,
    });
    await expect(
      accounting.modifyLock(
        FORCED_LOCK_ID,
        50n,
        FAR_FUTURE_EXPIRY,
        nonce,
        signature,
      ),
    ).to.be.revertedWithCustomError(accounting, 'BridgeAssetNotSupported');
  });

  it('withdrawFromLock reverts with BridgeAssetNotSupported on a forced BridgeAsset lock', async () => {
    const [, , , dest] = await ethers.getSigners();
    await expect(
      accounting.withdrawFromLock(
        userAddr,
        dest.address,
        FORCED_LOCK_ID,
        50n,
        0n,
        '0x',
      ),
    ).to.be.revertedWithCustomError(accounting, 'BridgeAssetNotSupported');
  });
});
