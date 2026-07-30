import type { HardhatRuntimeEnvironment } from "hardhat/types";
import type { CompilerInput } from "hardhat/types";
import erc1967ProxyBuildInfo from "@openzeppelin/upgrades-core/artifacts/build-info-v5.json";

// Hardhat's bundled `verify:sourcify` task (hardhat-verify v2) only talks to Sourcify's
// deprecated v1 API. Sourcify now requires the v2 API (https://docs.sourcify.dev/docs/api-v2),
// so we call it directly using the standard-json input Hardhat already saved during compile.
const SOURCIFY_API_URL = "https://sourcify.dev/server";
const VERIFICATION_POLL_INTERVAL_MS = 3000;
const VERIFICATION_TIMEOUT_MS = 120_000;

async function verifySourcify(
  hre: HardhatRuntimeEnvironment,
  address: string,
  fullyQualifiedName: string,
  stdJsonInput: CompilerInput,
  solcLongVersion: string
): Promise<void> {
  const chainId = (await hre.ethers.provider.getNetwork()).chainId.toString();

  const verifyResponse = await fetch(`${SOURCIFY_API_URL}/v2/verify/${chainId}/${address}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      stdJsonInput,
      contractIdentifier: fullyQualifiedName,
      compilerVersion: `v${solcLongVersion}`,
    }),
  });
  const verifyBody = await verifyResponse.json();
  if (!verifyResponse.ok) {
    throw new Error(`Sourcify verification request failed: ${verifyBody.message ?? JSON.stringify(verifyBody)}`);
  }
  const verificationId: string = verifyBody.verificationId;

  const deadline = Date.now() + VERIFICATION_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const statusResponse = await fetch(`${SOURCIFY_API_URL}/v2/verify/${verificationId}`);
    const statusBody = await statusResponse.json();
    if (!statusResponse.ok) {
      throw new Error(`Sourcify status check failed: ${statusBody.message ?? JSON.stringify(statusBody)}`);
    }

    if (statusBody.isJobCompleted) {
      if (statusBody.error) {
        throw new Error(`Sourcify verification failed: ${statusBody.error.message}`);
      }
      console.log(`Sourcify verification of ${fullyQualifiedName} at ${address}: ${statusBody.contract.match}`);
      return;
    }

    await new Promise((resolve) => setTimeout(resolve, VERIFICATION_POLL_INTERVAL_MS));
  }

  throw new Error(
    `Sourcify verification of ${fullyQualifiedName} at ${address} timed out after ${VERIFICATION_TIMEOUT_MS / 1000}s (verificationId: ${verificationId})`
  );
}

// Verify one of this project's own contracts, using the build info Hardhat saved for it.
export async function verifySourcifyContract(hre: HardhatRuntimeEnvironment, address: string, contractName: string): Promise<void> {
  const artifact = await hre.artifacts.readArtifact(contractName);
  const fullyQualifiedName = `${artifact.sourceName}:${artifact.contractName}`;

  const buildInfo = await hre.artifacts.getBuildInfo(fullyQualifiedName);
  if (!buildInfo) {
    throw new Error(`No build info found for ${fullyQualifiedName}`);
  }

  await verifySourcify(hre, address, fullyQualifiedName, buildInfo.input, buildInfo.solcLongVersion);
}

// The UUPS proxy deployed by hardhat-upgrades is OpenZeppelin's stock ERC1967Proxy, which this
// project never compiles itself. Verify it using the exact build info hardhat-upgrades bundles
// and deploys from, so the bytecode (and thus the source match) is guaranteed to line up.
const ERC1967_PROXY_FQN = "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol:ERC1967Proxy";

export async function verifySourcifyProxy(hre: HardhatRuntimeEnvironment, address: string): Promise<void> {
  await verifySourcify(
    hre,
    address,
    ERC1967_PROXY_FQN,
    erc1967ProxyBuildInfo.input as CompilerInput,
    erc1967ProxyBuildInfo.solcLongVersion
  );
}
