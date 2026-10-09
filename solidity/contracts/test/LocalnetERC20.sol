// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/**
 * @title LocalnetERC20
 * @notice 18-decimal ERC20 for the sapphire-localnet dev harness (chain 23293).
 * @dev Stands in for HONOR on Sapphire testnet so the local dev loop exercises the same-chain
 *      deposit path — accounting chain *is* the source chain — that `CHAIN_CONFIGS[23293]`
 *      (src/config/chain_config.py) and `ACCOUNTING_TOKEN_INFO` in `.env.localnet` expect.
 *      Localnet only; deployed at a fixed address by the `deploy-localnet-token` task
 *      (tasks/localnetToken.ts).
 */
contract LocalnetERC20 is ERC20 {
    /// @notice Deployer: the fixed localnet key from tasks/localnetToken.ts, and the only faucet.
    address public immutable owner;

    constructor(address initialHolder, uint256 initialSupply) ERC20("Localnet Honor", "LHONOR") {
        owner = msg.sender;
        _mint(initialHolder, initialSupply);
    }

    /**
     * @notice Faucet mint, deployer-only: funds deposit addresses while exercising the
     *         deposit → sweep → credit → withdraw flow. Localnet only.
     */
    function mint(address to, uint256 amount) external {
        require(msg.sender == owner, "LocalnetERC20: faucet only");
        _mint(to, amount);
    }
}
