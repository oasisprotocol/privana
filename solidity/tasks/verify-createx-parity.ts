// Manual one-off check: prove our local `_guard` mirror produces a salt that,
// when fed to the deployed CreateX `computeCreate3Address`, yields a different
// address than passing the raw salt would. Not wired into the regular test
// loop — invoke directly against a network that has CreateX deployed:
//
//   npx hardhat verify-createx-parity --label "XRose:phase0" --network base-sepolia
//   npx hardhat verify-createx-parity --label "XRose:phase0" --byte21 01 --network base-sepolia
//
// Unit tests (test/CreateXAddress.ts) prove the helper formula matches the
// upstream `_guard` expression character-by-character. This task adds an
// on-chain sanity check that we are not silently passing the wrong salt.

import { task } from "hardhat/config";

import {
  CREATEX_ABI,
  CREATEX_ADDRESS,
  createXGuardedSalt,
  createXPermissionedSameAddressSalt,
} from "./deploy-bridge";

task("verify-createx-parity", "Sanity-check local _guard helper against deployed CreateX")
  .addParam("label", "Salt label, e.g. 'XRose:phase0'")
  .addOptionalParam("deployer", "Override deployer (default: first signer)")
  .addOptionalParam("byte21", "Salt byte 21 — '00' (default) or '01' for tests", "00")
  .setAction(async (args, hre) => {
    const flag = String(args.byte21).toLowerCase();
    if (flag !== "00" && flag !== "01") {
      throw new Error(`--byte21 must be '00' or '01', got '${args.byte21}'`);
    }

    const [signer] = await hre.ethers.getSigners();
    const deployer = hre.ethers.getAddress(args.deployer ?? signer.address);
    const { chainId } = await hre.ethers.provider.getNetwork();

    const code = await hre.ethers.provider.getCode(CREATEX_ADDRESS);
    if (code === "0x") {
      throw new Error(
        `CreateX not deployed at ${CREATEX_ADDRESS} on chainId ${chainId}. ` +
          `Use a network that has CreateX (Base Sepolia / Sapphire / etc.).`,
      );
    }

    let rawSalt = createXPermissionedSameAddressSalt(deployer, args.label);
    if (flag === "01") {
      const bytes = hre.ethers.getBytes(rawSalt);
      bytes[20] = 0x01;
      rawSalt = hre.ethers.hexlify(bytes);
    }

    const helperGuarded = createXGuardedSalt(rawSalt, deployer, chainId);
    const createx = new hre.ethers.Contract(CREATEX_ADDRESS, CREATEX_ABI, hre.ethers.provider);
    const predictedFromGuarded: string = await createx.computeCreate3Address(helperGuarded);
    const predictedFromRaw: string = await createx.computeCreate3Address(rawSalt);

    console.log(`network        : chainId=${chainId}`);
    console.log(`deployer       : ${deployer}`);
    console.log(`label          : ${args.label}`);
    console.log(`byte21         : 0x${flag}`);
    console.log(`rawSalt        : ${rawSalt}`);
    console.log(`helperGuarded  : ${helperGuarded}`);
    console.log(`predicted(raw) : ${predictedFromRaw}`);
    console.log(`predicted(grdd): ${predictedFromGuarded}`);

    if (predictedFromGuarded === predictedFromRaw) {
      throw new Error(
        "parity FAIL: computeCreate3Address(rawSalt) == computeCreate3Address(helperGuarded). " +
          "Either the helper is a no-op transformation or the salt layout is wrong.",
      );
    }
    console.log("parity OK");
  });
