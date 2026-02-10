import { task } from "hardhat/config";

task("transfer")
  .addParam("contract", "The address of the Accounting contract")
  .addParam("to", "Recipient address")
  .addParam("tokenid", "Token ID (32-byte hex)")
  .addParam("amount", "Amount in token base units (e.g., 1000000 for 1 USDC)")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://p8000.m1356.opf-testnet-rofl-25.rofl.app",
  )
  .setDescription("Transfer funds from your account to another account")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = hre.ethers.getAddress(signer.address); // Ensure checksum
    const toAddress = hre.ethers.getAddress(args.to); // Ensure checksum
    const accounting = await hre.ethers.getContractAt(
      "Accounting",
      args.contract,
    );

    console.log("From address:", userAddress);
    console.log("To address:", toAddress);
    console.log("Token ID:", args.tokenid);
    console.log("Amount (base units):", args.amount);

    // Get balance before transfer
    const balanceUrl = `${args.apiurl}/v1/accounting/balances/${userAddress}/${args.tokenid}`;
    const balanceBefore = await fetch(balanceUrl)
      .then((r) => r.json())
      .then((d) => BigInt(d.balance || "0"))
      .catch(() => BigInt(0));
    console.log("Balance before:", balanceBefore.toString());

    // Get EIP-712 domain from contract
    const domainTuple = await accounting.eip712Domain();
    const domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };

    console.log("\nEIP-712 Domain:", domain);

    // Get the current transfer nonce for the user
    const nonce = await accounting.transferNonces(userAddress);
    console.log("Transfer nonce:", nonce.toString());

    // Define Transfer type (now includes nonce)
    const types = {
      Transfer: [
        { name: "userAddress", type: "address" },
        { name: "toAddress", type: "address" },
        { name: "tokenId", type: "bytes32" },
        { name: "amount", type: "uint256" },
        { name: "nonce", type: "uint256" },
      ],
    };

    // Create message
    const message = {
      userAddress: userAddress,
      toAddress: toAddress,
      tokenId: args.tokenid,
      amount: BigInt(args.amount),
      nonce: nonce,
    };

    console.log("\nMessage to sign:", JSON.stringify({
      userAddress: message.userAddress,
      toAddress: message.toAddress,
      tokenId: message.tokenId,
      amount: message.amount.toString(),
      nonce: message.nonce.toString(),
    }, null, 2));

    console.log("\nSigning transfer request...");

    // Sign the typed data
    const signature = await signer.signTypedData(domain, types, message);
    console.log("Signature:", signature);

    // Submit to API
    const apiUrl = `${args.apiurl}/v1/accounting/funds/transfer`;
    const payload = {
      user_address: userAddress,
      to_address: toAddress,
      token_id: args.tokenid,
      amount: args.amount,
      nonce: nonce.toString(),
      signature: signature,
    };

    console.log("\nPayload:", JSON.stringify(payload, null, 2));
    console.log("\nSubmitting transfer request to API...");

    const response = await fetch(apiUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });

    const responseText = await response.text();

    if (!response.ok) {
      console.error("\nAPI Error:", response.status, response.statusText);
      console.error("Response:", responseText);
      throw new Error(`API request failed: ${response.status}`);
    }

    const result = JSON.parse(responseText);
    console.log("\nTransfer submitted successfully!");
    console.log("Submission ID:", result.submission_id);

    // Check balance after (wait a bit for processing)
    console.log("\nWaiting for transaction to process...");
    await new Promise((r) => setTimeout(r, 5000));

    const balanceAfter = await fetch(balanceUrl)
      .then((r) => r.json())
      .then((d) => BigInt(d.balance || "0"))
      .catch(() => BigInt(0));
    console.log("Balance after:", balanceAfter.toString());

    return result;
  });
