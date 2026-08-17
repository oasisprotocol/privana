// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/**
 * @title LocalnetERC20
 * @notice 18-decimal ERC20 for the sapphire-localnet dev harness (chain 23293).
 * @dev Mirrors HONOR on Sapphire testnet (18 decimals) so the daily dev loop
 *      exercises the same-chain deposit path — accounting chain *is* the source
 *      chain — that `CHAIN_CONFIGS[23293]` (src/config/chain_config.py) and
 *      `ACCOUNTING_TOKEN_INFO` in `.env.localnet` are configured for.
 *
 *      Deployed by the `deploy-localnet-token` Hardhat task from a fixed key at
 *      nonce 0, which pins its CREATE address across localnet resets so the
 *      `.env.localnet` entry does not rot. See tasks/localnetToken.ts.
 *
 *      Localnet only — never deployed to Sapphire testnet or mainnet. `mint` is
 *      deliberately unpermissioned: it is the faucet used to fund deposit
 *      addresses while testing the deposit → sweep → credit → withdraw flow.
 */
contract LocalnetERC20 is ERC20 {
    constructor(address initialHolder, uint256 initialSupply) ERC20("Localnet Honor", "LHONOR") {
        _mint(initialHolder, initialSupply);
    }

    /**
     * @notice Mint tokens to any address. Localnet faucet — no access control.
     * @param to Recipient of the freshly minted tokens.
     * @param amount Amount in base units (18 decimals).
     */
    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}
