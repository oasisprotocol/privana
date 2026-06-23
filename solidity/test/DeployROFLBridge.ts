// Integration tests for the ROFLBridge CREATE3 deploy path against an in-process CreateX (hardhat_setCode). Covers:
//   - fresh deploy lands at the predicted address; constructor args verified
//     via on-chain reads (xrose/roflSigner/pauseAdmin).
//   - second run is idempotent.
//   - wrong xROSE constructor arg lands at the *same* predicted address
//     (CREATE3 ignores initcode for address derivation) but
//     `assertRoflBridgeWiring` catches the mismatch.
//   - tampered raw salt deploys elsewhere (salt drives the address).
//   - post-check fails when the predicted argument doesn't match what
//     CreateX actually deploys.

import { expect } from "chai";
import { artifacts, ethers, network } from "hardhat";
import { HardhatEthersSigner } from "@nomicfoundation/hardhat-ethers/signers";

import {
  CREATEX_ABI,
  CREATEX_ADDRESS,
  assertRoflBridgeWiring,
  buildRoflBridgeInitCode,
  buildXrosePreflight,
  deployCreate3IfMissing,
} from "../tasks/deploy-bridge";
import { installCreateX } from "./util/createx";

describe("DeployROFLBridge", () => {
  before(function () {
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });
  let deployer: HardhatEthersSigner;
  let customRoflSigner: HardhatEthersSigner;
  let customPauseAdmin: HardhatEthersSigner;
  let baselineSnapshot: string;

  const LABEL = "XRose:phase0";
  const NAME = "XRose";
  const SYMBOL = "xROSE";

  before(async () => {
    await installCreateX();
    [deployer, customRoflSigner, customPauseAdmin] = await ethers.getSigners();
    baselineSnapshot = await network.provider.send("evm_snapshot", []);
  });

  afterEach(async () => {
    await network.provider.send("evm_revert", [baselineSnapshot]);
    baselineSnapshot = await network.provider.send("evm_snapshot", []);
  });

  // Builds every prediction-derived value a bridge deploy needs: the xROSE
  // initcode/predicted address (so we can both deploy xROSE and feed its
  // predicted address into the bridge initcode), the bridge salts, the
  // bridge initcode, and a signer-bound CreateX contract.
  async function makePredictions(
    label: string,
    roflSigner: string,
    pauseAdmin: string,
    overrides?: { bridgeXroseArg?: string },
  ) {
    const xroseArtifact = await artifacts.readArtifact("XRose");
    const bridgeArtifact = await artifacts.readArtifact("ROFLBridge");
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
    const predictedXrose = ethers.getAddress(
      await createx.computeCreate3Address(predictions.xroseGuardedSalt),
    );
    const predictedBridge = ethers.getAddress(
      await createx.computeCreate3Address(predictions.roflBridgeGuardedSalt),
    );
    const bridgeXroseArg = overrides?.bridgeXroseArg ?? predictedXrose;
    const bridgeInitCode = buildRoflBridgeInitCode(
      { bytecode: bridgeArtifact.bytecode },
      bridgeXroseArg,
      roflSigner,
      pauseAdmin,
      deployer.address,
    );
    return { predictions, predictedXrose, predictedBridge, bridgeInitCode, createx };
  }

  // Deploys xROSE at the predicted address so a subsequent bridge deploy has
  // a real contract behind `predictedXrose`. Returns the predicted address.
  async function deployXroseFixture(predictions: ReturnType<typeof buildXrosePreflight>, predictedXrose: string, createx: ethers.Contract) {
    const result = await deployCreate3IfMissing(
      "xROSE",
      createx,
      ethers.provider,
      predictedXrose,
      predictions.xroseRawSalt,
      predictions.xroseInitCode,
    );
    expect(result.reused).to.equal(false);
    return result.address;
  }

  it("fresh deploy lands at the predicted bridge address with correct wiring", async () => {
    const roflSigner = customRoflSigner.address;
    const pauseAdmin = customPauseAdmin.address;
    const { predictions, predictedXrose, predictedBridge, bridgeInitCode, createx } =
      await makePredictions(LABEL, roflSigner, pauseAdmin);

    await deployXroseFixture(predictions, predictedXrose, createx);
    expect(await ethers.provider.getCode(predictedBridge)).to.equal("0x");

    const result = await deployCreate3IfMissing(
      "ROFLBridge",
      createx,
      ethers.provider,
      predictedBridge,
      predictions.roflBridgeRawSalt,
      bridgeInitCode,
    );
    expect(result.reused).to.equal(false);
    expect(result.address).to.equal(predictedBridge);
    expect(result.txHash)
      .to.be.a("string")
      .and.to.match(/^0x[0-9a-f]{64}$/i);

    const bridge = await ethers.getContractAt("ROFLBridge", predictedBridge);
    expect(await bridge.xrose()).to.equal(predictedXrose);
    expect(await bridge.roflSigner()).to.equal(roflSigner);
    expect(await bridge.pauseAdmin()).to.equal(pauseAdmin);

    await assertRoflBridgeWiring(bridge, {
      xrose: predictedXrose,
      roflSigner,
      pauseAdmin,
      owner: deployer.address,
    });
  });

  it("second run is a no-op (idempotency)", async () => {
    const roflSigner = customRoflSigner.address;
    const pauseAdmin = customPauseAdmin.address;
    const { predictions, predictedXrose, predictedBridge, bridgeInitCode, createx } =
      await makePredictions(LABEL, roflSigner, pauseAdmin);

    await deployXroseFixture(predictions, predictedXrose, createx);

    const first = await deployCreate3IfMissing(
      "ROFLBridge",
      createx,
      ethers.provider,
      predictedBridge,
      predictions.roflBridgeRawSalt,
      bridgeInitCode,
    );
    expect(first.reused).to.equal(false);

    const second = await deployCreate3IfMissing(
      "ROFLBridge",
      createx,
      ethers.provider,
      predictedBridge,
      predictions.roflBridgeRawSalt,
      bridgeInitCode,
    );
    expect(second.reused).to.equal(true);
    expect(second.txHash).to.equal(null);
    expect(second.address).to.equal(predictedBridge);

    // Wiring still verifies on the reused path — exactly the guarantee the
    // production task body needs when re-running over an existing deploy.
    const bridge = await ethers.getContractAt("ROFLBridge", predictedBridge);
    await assertRoflBridgeWiring(bridge, {
      xrose: predictedXrose,
      roflSigner,
      pauseAdmin,
      owner: deployer.address,
    });
  });

  it("wrong xROSE constructor arg → same predicted address, wiring assertion fails", async () => {
    const roflSigner = customRoflSigner.address;
    const pauseAdmin = customPauseAdmin.address;
    const wrongXrose = customRoflSigner.address; // any non-zero address that isn't predictedXrose

    const correct = await makePredictions(LABEL, roflSigner, pauseAdmin);
    await deployXroseFixture(correct.predictions, correct.predictedXrose, correct.createx);

    const tampered = await makePredictions(LABEL, roflSigner, pauseAdmin, {
      bridgeXroseArg: wrongXrose,
    });
    // CREATE3 derives the address from `(salt, deployer)` only — feeding a
    // different `bridgeXroseArg` into the initcode must NOT change the
    // predicted address. Sanity-check that off-chain expectation before we
    // deploy.
    expect(tampered.predictedBridge).to.equal(correct.predictedBridge);

    const result = await deployCreate3IfMissing(
      "ROFLBridge",
      tampered.createx,
      ethers.provider,
      tampered.predictedBridge,
      tampered.predictions.roflBridgeRawSalt,
      tampered.bridgeInitCode,
    );
    expect(result.reused).to.equal(false);
    expect(result.address).to.equal(correct.predictedBridge);

    const bridge = await ethers.getContractAt("ROFLBridge", correct.predictedBridge);
    expect(await bridge.xrose()).to.equal(ethers.getAddress(wrongXrose));

    await expect(
      assertRoflBridgeWiring(bridge, {
        xrose: correct.predictedXrose,
        roflSigner,
        pauseAdmin,
        owner: deployer.address,
      }),
    ).to.be.rejectedWith(/ROFLBridge wiring: xrose mismatch/);
  });

  it("tampered raw salt deploys to a different bridge address (salt drives address)", async () => {
    const roflSigner = customRoflSigner.address;
    const pauseAdmin = customPauseAdmin.address;
    const original = await makePredictions(LABEL, roflSigner, pauseAdmin);
    const tampered = await makePredictions("XRose:tampered", roflSigner, pauseAdmin);

    expect(tampered.predictedBridge).to.not.equal(original.predictedBridge);
    // xROSE addresses also diverge — each test uses its own xROSE so the
    // bridge has a real contract behind its constructor arg.
    expect(tampered.predictedXrose).to.not.equal(original.predictedXrose);

    await deployXroseFixture(
      tampered.predictions,
      tampered.predictedXrose,
      tampered.createx,
    );

    const result = await deployCreate3IfMissing(
      "ROFLBridge",
      tampered.createx,
      ethers.provider,
      tampered.predictedBridge,
      tampered.predictions.roflBridgeRawSalt,
      tampered.bridgeInitCode,
    );
    expect(result.address).to.equal(tampered.predictedBridge);
    expect(result.address).to.not.equal(original.predictedBridge);
    expect(await ethers.provider.getCode(tampered.predictedBridge)).to.not.equal("0x");
    expect(await ethers.provider.getCode(original.predictedBridge)).to.equal("0x");
  });

  it("throws if predicted argument doesn't match where CreateX actually deploys", async () => {
    const roflSigner = customRoflSigner.address;
    const pauseAdmin = customPauseAdmin.address;
    const { predictions, predictedXrose, bridgeInitCode, createx } =
      await makePredictions(LABEL, roflSigner, pauseAdmin);

    await deployXroseFixture(predictions, predictedXrose, createx);

    const wrongPredicted = "0x0000000000000000000000000000000000005678";

    await expect(
      deployCreate3IfMissing(
        "ROFLBridge",
        createx,
        ethers.provider,
        wrongPredicted,
        predictions.roflBridgeRawSalt,
        bridgeInitCode,
      ),
    ).to.be.rejectedWith(/predicted address .* has no code/);
  });
});
