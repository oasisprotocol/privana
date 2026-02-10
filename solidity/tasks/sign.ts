import { task } from "hardhat/config";

task("sign")
  .addParam("contract", "The address of the Accounting contract")
  .addParam("type", "Signature type: lock, transfer, transferlocked, or withdraw")
  .addParam("user", "User address")
  .addParam("amount", "Amount in ether units (e.g., '1' for 1 token)")
  .addOptionalParam("tokenid", "Token ID (32-byte hex, required for lock/transfer/withdraw)")
  .addOptionalParam("to", "Recipient address (required for transfer/transferlocked)")
  .addOptionalParam("service", "Service address (required for lock)")
  .addOptionalParam("expiry", "Lock expiry timestamp (optional for lock, defaults to 1 hour from now)")
  .addOptionalParam("lockid", "Lock ID (required for transferlocked)")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const accounting = await hre.ethers.getContractAt("Accounting", args.contract);

    const domainTuple = await accounting.eip712Domain();
    const domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };

    const amountWei = hre.ethers.parseEther(args.amount);
    const signatureType = args.type.toLowerCase();
    let types: any;
    let message: any;

    switch (signatureType) {
      case "lock":
        if (!args.tokenid || !args.service) {
          throw new Error("Lock requires: tokenid and service");
        }

        const expiry = args.expiry || Math.floor(Date.now() / 1000) + (60 * 60);

        types = {
          Lock: [
            { name: "userAddress", type: "address" },
            { name: "serviceAddress", type: "address" },
            { name: "tokenId", type: "bytes32" },
            { name: "amount", type: "uint256" },
            { name: "expiry", type: "uint256" },
          ]
        };
        message = {
          userAddress: args.user,
          serviceAddress: args.service,
          tokenId: args.tokenid,
          amount: amountWei,
          expiry: expiry,
        };
        console.log(`Expiry: ${expiry} (${new Date(expiry * 1000).toISOString()})`);
        break;

      case "transfer":
        if (!args.tokenid || !args.to) {
          throw new Error("Transfer requires: tokenid and to");
        }
        const transferNonce = await accounting.transferNonces(args.user);
        types = {
          Transfer: [
            { name: "userAddress", type: "address" },
            { name: "toAddress", type: "address" },
            { name: "tokenId", type: "bytes32" },
            { name: "amount", type: "uint256" },
            { name: "nonce", type: "uint256" },
          ]
        };
        message = {
          userAddress: args.user,
          toAddress: args.to,
          tokenId: args.tokenid,
          amount: amountWei,
          nonce: transferNonce,
        };
        console.log(`Transfer nonce: ${transferNonce}`);
        break;

      case "transferlocked":
        if (!args.to || args.lockid === undefined) {
          throw new Error("TransferLocked requires: to and lockid");
        }
        types = {
          TransferLocked: [
            { name: "userAddress", type: "address" },
            { name: "toAddress", type: "address" },
            { name: "lockId", type: "uint256" },
            { name: "amount", type: "uint256" },
          ]
        };
        message = {
          userAddress: args.user,
          toAddress: args.to,
          lockId: args.lockid,
          amount: amountWei,
        };
        break;

      case "withdraw":
        if (!args.tokenid) {
          throw new Error("Withdraw requires: tokenid");
        }
        types = {
          Withdraw: [
            { name: "userAddress", type: "address" },
            { name: "tokenId", type: "bytes32" },
            { name: "amount", type: "uint256" },
          ]
        };
        message = {
          userAddress: args.user,
          tokenId: args.tokenid,
          amount: amountWei,
        };
        break;

      default:
        throw new Error(`Unknown signature type: ${signatureType}. Valid types: lock, transfer, transferlocked, withdraw`);
    }

    console.log(`Amount (wei): ${amountWei}`);
    const signature = await signer.signTypedData(domain, types, message);
    console.log(`Signature: ${signature}`);
    return signature;
  });
