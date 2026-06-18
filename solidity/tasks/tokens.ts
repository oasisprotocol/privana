import { task } from "hardhat/config";
import {
  fetchBalance,
  getSiweToken,
  normalizeApiBaseUrl,
} from "./utils/siwe";

task("getBalance")
  .addParam("tokenid", "Token ID (32-byte hex)")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://api.testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "84532")
  .setDescription("Get user balance for a token (requires SIWE authentication)")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = signer.address;
    const apiBaseUrl = normalizeApiBaseUrl(args.apiurl);
    const chainId = parseInt(args.chainid);

    console.log("User address:", userAddress);
    console.log("Token ID:", args.tokenid);
    console.log("API URL:", apiBaseUrl);

    console.log("\nAuthenticating with SIWE...");
    const siweToken = await getSiweToken({
      apiBaseUrl,
      signer,
      userAddress,
      chainId,
    });
    console.log("SIWE authentication successful");

    console.log("\nFetching balance...");
    const balance = await fetchBalance({
      apiBaseUrl,
      userAddress,
      tokenId: args.tokenid,
      siweToken,
    });

    console.log("\n=== Balance ===");
    console.log("Raw (base units):", balance.toString());
    // Assume 6 decimals for USDC-like tokens, can be parameterized
    console.log("Formatted (6 decimals):", (Number(balance) / 1e6).toFixed(6));

    return balance;
  });

task("transferERC20")
  .addOptionalParam("token", "ERC20 token address", "0x12084e1a0fe92b5ab803a81a0ae54d91040f89ca")
  .addOptionalParam("recipient", "Recipient address", "0x284a3Fe2939a4e4859e6321537d4264533E3D549")
  .addOptionalParam("amount", "Amount in token units", "1")
  .addOptionalParam("decimals", "Token decimals", "18")
  .setAction(async (args, hre) => {
    const tokenAddress = args.token;
    const recipient = args.recipient;
    const amount = hre.ethers.parseUnits(args.amount, parseInt(args.decimals));

    const [signer] = await hre.ethers.getSigners();
    console.log("Sender address:", signer.address);

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
    console.log("Type:", receipt?.type, "(should be 2 for EIP-1559)");

    const rawReceipt = await hre.ethers.provider.send("eth_getTransactionReceipt", [tx.hash]);
    console.log("\nRaw receipt status:", rawReceipt.status);
    console.log("Receipt type field:", rawReceipt.type);

    console.log("\n=== Transaction details ===");
    console.log("Block number:", receipt?.blockNumber);
    console.log("Transaction index:", receipt?.index);
    console.log("User address:", signer.address);
    console.log("Transaction hash:", tx.hash);

    return tx.hash;
  });

task("getAuthKeyHash")
  .addParam("address", "The address of the SIWE Auth contract")
  .setDescription("Get the hash of the stored encryption key from the SIWE Auth contract")
  .setAction(async (args, hre) => {
    const siweAuthContract = new hre.ethers.Contract(
      args.address,
      ["function getAuthTokenEncKeyHash() external view returns (bytes32)"],
      hre.ethers.provider
    );

    const keyHash = await siweAuthContract.getAuthTokenEncKeyHash();
    console.log("Encryption key hash on contract:", keyHash);
    return keyHash;
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
