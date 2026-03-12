import { task } from "hardhat/config";
import { AbiCoder, keccak256, solidityPacked } from "ethers";

// Compute token ID: keccak256(abi.encode(tokenType, abi.encodePacked(chainId, tokenAddress)))
function computeErc20TokenId(chainId: bigint, tokenAddress: string): string {
  const abiCoder = new AbiCoder();
  const tokenType = 1; // ERC20
  // abi.encodePacked(chainId, tokenAddress) = 32 bytes + 20 bytes = 52 bytes
  const data = solidityPacked(["uint256", "address"], [chainId, tokenAddress]);
  return keccak256(abiCoder.encode(["uint8", "bytes"], [tokenType, data]));
}

task("transferERC20")
  .addOptionalParam("token", "ERC20 token address", "0x036CbD53842c5426634e7929541eC2318f3dCF7e")
  .addOptionalParam("recipient", "Recipient address", "0xb420ab8484cf72ddf73e014c7800984c85604ef4")
  .addOptionalParam("amount", "Amount in token units", "1")
  .addOptionalParam("decimals", "Token decimals", "6")
  .addOptionalParam("apiurl", "Accounting API base URL", "https://p8000.m1356.opf-testnet-rofl-25.rofl.app")
  .addOptionalParam("timeout", "Timeout in seconds to wait for deposit", "120")
  .setAction(async (args, hre) => {
    const tokenAddress = args.token;
    const recipient = args.recipient;
    const amount = hre.ethers.parseUnits(args.amount, parseInt(args.decimals));

    const [signer] = await hre.ethers.getSigners();
    console.log("Sender address:", signer.address);

    // Compute token ID from chain ID and token address
    const network = await hre.ethers.provider.getNetwork();
    const tokenId = computeErc20TokenId(network.chainId, tokenAddress);
    console.log("Token ID:", tokenId);

    const feeData = await hre.ethers.provider.getFeeData();
    console.log("Fee data:", {
      maxFeePerGas: feeData.maxFeePerGas?.toString(),
      maxPriorityFeePerGas: feeData.maxPriorityFeePerGas?.toString(),
      gasPrice: feeData.gasPrice?.toString(),
    });

    const erc20 = new hre.ethers.Contract(
      tokenAddress,
      [
        "function transfer(address to, uint256 amount) returns (bool)",
        "function balanceOf(address) view returns (uint256)",
      ],
      signer
    );

    const balance = await erc20.balanceOf(signer.address);
    console.log("Token balance:", hre.ethers.formatUnits(balance, parseInt(args.decimals)));

    if (balance < amount) {
      throw new Error("Insufficient token balance!");
    }

    // Get accounting balance before transfer
    let balanceBefore = BigInt(0);
    const balanceUrl = `${args.apiurl}/v1/accounting/balances/${signer.address}/${tokenId}`;
    console.log("Balance URL:", balanceUrl);
    try {
      const resp = await fetch(balanceUrl);
      if (!resp.ok) {
        const text = await resp.text();
        console.log(`API error ${resp.status}: ${text}`);
      } else {
        const data = await resp.json();
        balanceBefore = BigInt(data.balance || "0");
        console.log("Accounting balance before:", balanceBefore.toString());
      }
    } catch (e) {
      console.log("Could not fetch accounting balance:", e);
    }

    console.log("\nSending EIP-1559 (type 2) transaction...");
    console.log(`Token: ${tokenAddress}`);
    console.log(`Recipient: ${recipient}`);
    console.log(`Amount: ${args.amount} (${amount.toString()} wei)`);

    const tx = await erc20.transfer(recipient, amount, {
      maxFeePerGas: feeData.maxFeePerGas,
      maxPriorityFeePerGas: feeData.maxPriorityFeePerGas,
      type: 2,
    });

    console.log("Transaction hash:", tx.hash);
    console.log("Waiting for confirmation...");

    const receipt = await tx.wait();

    console.log("\n=== Transaction Confirmed ===");
    console.log("Block number:", receipt?.blockNumber);
    console.log("Transaction index:", receipt?.index);
    console.log("Gas used:", receipt?.gasUsed.toString());

    // Wait for deposit to be credited
    console.log("\n=== Waiting for deposit to be credited ===");
    const timeoutMs = parseInt(args.timeout) * 1000;
    const startTime = Date.now();

    while (Date.now() - startTime < timeoutMs) {
      try {
        const resp = await fetch(balanceUrl);
        const data = await resp.json();
        const currentBalance = BigInt(data.balance || "0");

        if (currentBalance > balanceBefore) {
          const credited = currentBalance - balanceBefore;
          console.log("\n=== Deposit Credited ===");
          console.log("Balance before:", balanceBefore.toString());
          console.log("Balance after:", currentBalance.toString());
          console.log("Credited:", credited.toString());
          return tx.hash;
        }
      } catch {
        // API error, keep polling
      }

      process.stdout.write(".");
      await new Promise((r) => setTimeout(r, 3000));
    }

    console.log("\n\nTimeout waiting for deposit to be credited.");
    console.log("The deposit may still be processing in the background.");

    return tx.hash;
  });

task("addEVMNativeToken")
  .addParam("address", "The address of the Accounting contract")
  .addParam("chainid", "Chain ID")
  .setAction(async (args, hre) => {
    const accountingAddr = args.address;
    const accounting = await hre.ethers.getContractAt("Accounting", accountingAddr);
    const tokenType = 0;
    const chainId = args.chainid;
    const data = await accounting.encodeEVMNativeTokenData(chainId);
    const tx = await accounting.setTokenInfo({
      tokenType,
      data,
    });
    console.log(`Transaction hash: ${tx.hash}`);
    const receipt = await tx.wait();
    console.log(`Transaction confirmed in block ${receipt?.blockNumber}`);
    const tokenId = await accounting.getTokenId({
      tokenType,
      data,
    });
    console.log(`Token ID: ${tokenId}`);
  });

task("addEVMErc20Token")
  .addParam("address", "The address of the Accounting contract")
  .addParam("chainid", "Chain ID")
  .addParam("tokenaddress", "ERC20 token address")
  .setAction(async (args, hre) => {
    const accountingAddr = args.address;
    const accounting = await hre.ethers.getContractAt("Accounting", accountingAddr);
    const tokenType = 1;
    const chainId = args.chainid;
    const tokenAddress = args.tokenaddress;
    const data = await accounting.encodeEVMErc20TokenData(chainId, tokenAddress);
    const tx = await accounting.setTokenInfo({
      tokenType,
      data,
    });
    console.log(`Transaction hash: ${tx.hash}`);
    const receipt = await tx.wait();
    console.log(`Transaction confirmed in block ${receipt?.blockNumber}`);
    const tokenId = await accounting.getTokenId({
      tokenType,
      data,
    });
    console.log(`Token ID: ${tokenId}`);
  });
