// Integration tests for deployCreate3IfMissing (xROSE path) against an in-process CreateX (hardhat_setCode). Covers:
//   - fresh deploy lands at the predicted address
//   - second run is a no-op (idempotency)
//   - tampered raw salt deploys elsewhere (salt drives the address)
//   - post-check fails when predicted argument doesn't match CreateX's deploy
//
// CreateX runtime bytecode is vendored at solidity/test/fixtures/createx-runtime.json
// (post-constructor runtime from a chain where CreateX is live — see
// test/util/createx.ts for the rationale around `_SELF` immutables).

import { expect } from "chai";
import { artifacts, ethers, network } from "hardhat";
import { HardhatEthersSigner } from "@nomicfoundation/hardhat-ethers/signers";

import {
  CREATEX_ABI,
  CREATEX_ADDRESS,
  buildXrosePreflight,
  createXGuardedSalt,
  createXPermissionedSameAddressSalt,
  deployCreate3IfMissing,
} from "../tasks/deploy-bridge";
import { installCreateX } from "./util/createx";

describe("DeployXRose", () => {
  before(function () {
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });
  let deployer: HardhatEthersSigner;
  let baselineSnapshot: string;

  const LABEL = "XRose:phase0";
  const NAME = "XRose";
  const SYMBOL = "xROSE";

  before(async () => {
    await installCreateX();
    [deployer] = await ethers.getSigners();
    baselineSnapshot = await network.provider.send("evm_snapshot", []);
  });

  afterEach(async () => {
    // Revert to the "CreateX installed, no xROSE deployed" baseline.
    await network.provider.send("evm_revert", [baselineSnapshot]);
    baselineSnapshot = await network.provider.send("evm_snapshot", []);
  });

  async function makePredictions(label: string) {
    const xroseArtifact = await artifacts.readArtifact("XRose");
    const { chainId } = await ethers.provider.getNetwork();
    const predictions = buildXrosePreflight({
      chainId,
      deployer: deployer.address,
      xroseName: NAME,
      xroseSymbol: SYMBOL,
      label,
      xroseArtifact: {
        bytecode: xroseArtifact.bytecode,
        deployedBytecode: xroseArtifact.deployedBytecode,
      },
    });
    const createx = new ethers.Contract(CREATEX_ADDRESS, CREATEX_ABI, deployer);
    const predicted: string = await createx.computeCreate3Address(
      predictions.xroseGuardedSalt,
    );
    return { predictions, predicted: ethers.getAddress(predicted), createx };
  }

  it("fresh deploy lands at the predicted address with correct constructor args", async () => {
    const { predictions, predicted, createx } = await makePredictions(LABEL);

    expect(await ethers.provider.getCode(predicted)).to.equal("0x");

    const result = await deployCreate3IfMissing(
      "xROSE",
      createx,
      ethers.provider,
      predicted,
      predictions.xroseRawSalt,
      predictions.xroseInitCode,
    );

    expect(result.reused).to.equal(false);
    expect(result.address).to.equal(predicted);
    expect(result.txHash)
      .to.be.a("string")
      .and.to.match(/^0x[0-9a-f]{64}$/i);

    const codeAfter = await ethers.provider.getCode(predicted);
    expect(codeAfter).to.not.equal("0x");

    const xrose = await ethers.getContractAt("XRose", predicted);
    expect(await xrose.name()).to.equal(NAME);
    expect(await xrose.symbol()).to.equal(SYMBOL);
    expect(await xrose.FACTORY()).to.equal(deployer.address);
    expect(await xrose.owner()).to.equal(deployer.address);
  });

  it("second run is a no-op (idempotency)", async () => {
    const { predictions, predicted, createx } = await makePredictions(LABEL);

    const first = await deployCreate3IfMissing(
      "xROSE",
      createx,
      ethers.provider,
      predicted,
      predictions.xroseRawSalt,
      predictions.xroseInitCode,
    );
    expect(first.reused).to.equal(false);

    const second = await deployCreate3IfMissing(
      "xROSE",
      createx,
      ethers.provider,
      predicted,
      predictions.xroseRawSalt,
      predictions.xroseInitCode,
    );

    expect(second.reused).to.equal(true);
    expect(second.txHash).to.equal(null);
    expect(second.address).to.equal(predicted);
  });

  it("tampered raw salt deploys to a different address (salt drives address)", async () => {
    const { predicted: originalPredicted } = await makePredictions(LABEL);
    const {
      predictions: tamperedPredictions,
      predicted: tamperedPredicted,
      createx,
    } = await makePredictions("XRose:tampered");

    expect(tamperedPredicted).to.not.equal(originalPredicted);

    // Sanity: salts diverge off-chain too.
    const rawForLabel = createXPermissionedSameAddressSalt(
      deployer.address,
      `${LABEL}:xrose`,
    );
    const rawForTampered = createXPermissionedSameAddressSalt(
      deployer.address,
      "XRose:tampered:xrose",
    );
    expect(rawForLabel).to.not.equal(rawForTampered);

    const result = await deployCreate3IfMissing(
      "xROSE",
      createx,
      ethers.provider,
      tamperedPredicted,
      tamperedPredictions.xroseRawSalt,
      tamperedPredictions.xroseInitCode,
    );

    expect(result.address).to.equal(tamperedPredicted);
    expect(result.address).to.not.equal(originalPredicted);
    expect(await ethers.provider.getCode(tamperedPredicted)).to.not.equal("0x");
    expect(await ethers.provider.getCode(originalPredicted)).to.equal("0x");
  });

  it("throws if predicted argument doesn't match where CreateX actually deploys", async () => {
    const { predictions, createx } = await makePredictions(LABEL);

    // Use a wrong predicted address. CreateX will deploy to the salt-derived
    // address; the helper's post-check then sees `predictedAddress` (the wrong
    // one) with no code and must throw.
    const wrongPredicted = "0x0000000000000000000000000000000000001234";

    await expect(
      deployCreate3IfMissing(
        "xROSE",
        createx,
        ethers.provider,
        wrongPredicted,
        predictions.xroseRawSalt,
        predictions.xroseInitCode,
      ),
    ).to.be.rejectedWith(/predicted address .* has no code/);
  });

  it("guardedSalt off-chain matches CreateX.computeCreate3Address(guarded)", async () => {
    // Sanity that the helper-required parity check the task does is sound:
    // off-chain `createXGuardedSalt` reproduces CreateX's internal `_guard`.
    const { predictions, createx, predicted } = await makePredictions(LABEL);
    const { chainId } = await ethers.provider.getNetwork();

    const guardedOffchain = createXGuardedSalt(
      predictions.xroseRawSalt,
      deployer.address,
      chainId,
    );
    expect(guardedOffchain).to.equal(predictions.xroseGuardedSalt);

    const predictedFromRaw: string = await createx.computeCreate3Address(
      predictions.xroseRawSalt,
    );
    const predictedFromGuarded: string = await createx.computeCreate3Address(
      predictions.xroseGuardedSalt,
    );
    expect(ethers.getAddress(predictedFromGuarded)).to.equal(predicted);
    // CreateX applies `_guard` on raw salts at deploy-time, so the guarded view
    // is what matches the actual deployed address. The raw view must differ —
    // this is the property `assertHelperRequired` enforces.
    expect(ethers.getAddress(predictedFromRaw)).to.not.equal(predicted);
  });
});
