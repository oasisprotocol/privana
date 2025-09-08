import { expect } from 'chai';
import { ethers } from 'hardhat';
import { parseEther, Wallet } from 'ethers';
import { Accounting } from '../typechain-types';
import { EthereumKeypairStruct } from '../typechain-types/contracts/Accounting';
// import {
//   isCalldataEnveloped,
//   wrapEthereumProvider,
// } from '@oasisprotocol/sapphire-paratime';

describe('Accounting', function () {
  let accounting: Accounting;

  before(async () => {
    const provider = ethers.provider;

    // Generate a random wallet for the gasless tx signing account
    const wallet = Wallet.createRandom(provider);
    const keypair: EthereumKeypairStruct = {
      addr: wallet.address,
      secret: wallet.privateKey,
      nonce: await provider.getTransactionCount(wallet.address),
    };

    // Any eth passed to constructor will be sent to the random wallet
    const AccountingFactory = await ethers.getContractFactory('Accounting');
    accounting = await AccountingFactory.deploy(keypair, {
      value: parseEther('0.1'),
    });
    console.log('    . deployed Accounting to', await accounting.getAddress());
    console.log('    . gasless pubkey', wallet.address);
  });
});
