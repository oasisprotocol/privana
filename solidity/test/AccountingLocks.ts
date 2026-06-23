import { expect } from "chai";
import { ethers, config, upgrades } from "hardhat";
import { Contract, Wallet } from "ethers";
import { HardhatNetworkHDAccountsConfig } from "hardhat/types";
import { MockAccounting } from "../typechain-types";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

// Non-bridge ERC20 token used by the lock test. createLock rejects BridgeAsset
// tokens (e.g. ROSE), so the path needs a plain ERC20.
const TokenType = { NativeEVM: 0, ERC20: 1, BridgeAsset: 2 } as const;
const TEST_TOKEN = {
  chainId: 84532,
  address: "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
  // keccak256(abi.encodePacked(uint256(84532), address(0x036c...cf7e)))
  tokenId: "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514",
};

const lockTypes = {
  Lock: [
    { name: "serviceAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "expiry", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
};

async function deployAccountingProxy(
  deployerAddress: string,
): Promise<MockAccounting> {
  const MockSiweAuthFactory = await ethers.getContractFactory("MockSiweAuth");
  const mockSiweAuth = await MockSiweAuthFactory.deploy("test");
  await mockSiweAuth.waitForDeployment();

  const AccountingFactory = await ethers.getContractFactory("MockAccounting");
  const accounting = (await upgrades.deployProxy(
    AccountingFactory,
    [MOCK_ROFL_APP_ID, deployerAddress],
    {
      kind: "uups",
      initializer: "initialize",
      constructorArgs: [await mockSiweAuth.getAddress()],
      unsafeAllow: [],
    },
  )) as unknown as MockAccounting;
  await accounting.waitForDeployment();
  return accounting;
}

function getUserWallet(): Wallet {
  const mnemonic = (
    config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig
  ).mnemonic;
  return ethers.HDNodeWallet.fromPhrase(
    mnemonic,
    undefined,
    "m/44'/60'/0'/0/0",
  ).connect(ethers.provider) as unknown as Wallet;
}

async function getDomain(accounting: Contract | MockAccounting) {
  const d = await (accounting as any).eip712Domain();
  return {
    name: d[1],
    version: d[2],
    chainId: Number(d[3]),
    verifyingContract: d[4],
  };
}

function mockAuthToken(address: string): string {
  return ethers.hexlify(ethers.zeroPadValue(address, 32));
}

async function getBlockTimestamp(): Promise<number> {
  const block = await ethers.provider.getBlock("latest");
  return block!.timestamp;
}

// ─── lock behavior ────────────────────────────────────────────────────────
//
// The lock primitives live on Accounting. They recover the EIP-712 signer with
// `ECDSA.recover` (no Sapphire precompile), so the real bodies run end-to-end
// in-memory on Hardhat.
describe("Accounting locks", () => {
  it("createLock decrements the user balance and lands a visible lock", async () => {
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);

    // Register a plain ERC20 token (createLock rejects BridgeAsset) and seed
    // the user's balance via the MockAccounting helper.
    const tokenData = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
      ethers.zeroPadValue(TEST_TOKEN.address, 20),
    ]);
    await (accounting as any).setTokenInfo({
      tokenType: TokenType.ERC20,
      data: tokenData,
    });

    const userWallet = getUserWallet();
    const seeded = 1_000_000n;
    const amount = 250_000n;
    await accounting.setBalance(userWallet.address, TEST_TOKEN.tokenId, seeded);

    const expiry = (await getBlockTimestamp()) + 3600;
    const domain = await getDomain(accounting);
    const nonce = await (accounting as any).createLockNonces(userWallet.address);
    const signature = await userWallet.signTypedData(domain, lockTypes, {
      serviceAddress: owner.address,
      tokenId: TEST_TOKEN.tokenId,
      amount,
      expiry,
      nonce,
    });

    await accounting.createLock(
      owner.address,
      TEST_TOKEN.tokenId,
      amount,
      expiry,
      nonce,
      signature,
    );

    expect(
      await accounting.getBalance(userWallet.address, TEST_TOKEN.tokenId),
    ).to.equal(seeded - amount);

    const locks = await accounting.getUserLocks(
      mockAuthToken(userWallet.address),
    );
    expect(locks.length).to.equal(1);
    expect(locks[0].serviceId).to.equal(owner.address);
    expect(locks[0].tokenId).to.equal(TEST_TOKEN.tokenId);
    expect(locks[0].amount).to.equal(amount);
    expect(locks[0].expiry).to.equal(BigInt(expiry));
  });
});
