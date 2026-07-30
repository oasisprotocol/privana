import { task } from "hardhat/config";
import { verifySourcifyContract, verifySourcifyProxy } from "./utils/sourcify";

// Overrides hardhat-verify's built-in `verify:sourcify` subtask (registered by
// @nomicfoundation/hardhat-toolbox), which only talks to Sourcify's deprecated v1 API. This
// replaces its action with our own v2-API implementation, reusing its existing `address` and
// `contract` params — Hardhat won't let an overridden task redeclare params that already
// exist on the parent. Since `verify` (the top-level task) internally runs `verify:sourcify`
// too, this override also fixes verification for anyone using the standard `verify` task.
task("verify:sourcify")
  .addFlag("proxy", "Verify as the ERC1967Proxy hardhat-upgrades deploys, using its bundled build info instead of --contract")
  .setDescription("Verify a contract on Sourcify using the v2 API")
  .setAction(async (args, hre) => {
    if (args.proxy) {
      await verifySourcifyProxy(hre, args.address);
      return;
    }

    if (!args.contract) {
      throw new Error("--contract <name> is required unless --proxy is set");
    }
    await verifySourcifyContract(hre, args.address, args.contract);
  });
