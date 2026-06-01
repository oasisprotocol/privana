// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IXERC20} from "./IXERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @notice Base-chain adapter that mints/burns xROSE under the ROFL custody EOA's authority.
contract ROFLBridge is Ownable {
    IXERC20 public immutable xrose;
    address public roflSigner;
    address public pauseAdmin;

    mapping(bytes32 => bool) public mintedWithdrawalIds;
    mapping(bytes32 => bool) public burnedDepositIds;

    bool private _paused;

    error Unauthorized();
    error EnforcedPause();
    error ExpectedPause();
    error AlreadyProcessed();
    error ZeroAddress();
    error ZeroAmount();
    error ZeroId();
    error InsufficientSweep(uint256 balance, uint256 amount);

    event Minted(bytes32 indexed withdrawalId, address indexed to, uint256 amount);
    event Burned(bytes32 indexed depositId, uint256 amount);
    event Paused(address account);
    event Unpaused(address account);
    event RoflSignerUpdated(address indexed previous, address indexed current);
    event PauseAdminUpdated(address indexed previous, address indexed current);

    modifier onlyROFL() {
        if (msg.sender != roflSigner) revert Unauthorized();
        _;
    }

    modifier onlyPauseAdmin() {
        if (msg.sender != pauseAdmin) revert Unauthorized();
        _;
    }

    modifier whenNotPaused() {
        if (_paused) revert EnforcedPause();
        _;
    }

    modifier whenPaused() {
        if (!_paused) revert ExpectedPause();
        _;
    }

    constructor(IXERC20 _xrose, address _roflSigner, address _pauseAdmin, address _owner)
        Ownable(_owner)
    {
        if (address(_xrose) == address(0)) revert ZeroAddress();
        if (_roflSigner == address(0)) revert ZeroAddress();
        if (_pauseAdmin == address(0)) revert ZeroAddress();
        xrose = _xrose;
        roflSigner = _roflSigner;
        pauseAdmin = _pauseAdmin;
    }

    /// @notice Rotate the ROFL signer authority. Owner-only; emits previous and
    ///         current. Pair this with re-signing pending operations on the
    ///         off-chain ROFL side — in-flight calls signed by the previous key
    ///         will revert with `Unauthorized` once this lands.
    function setRoflSigner(address newSigner) external onlyOwner {
        if (newSigner == address(0)) revert ZeroAddress();
        address previous = roflSigner;
        roflSigner = newSigner;
        emit RoflSignerUpdated(previous, newSigner);
    }

    /// @notice Rotate the pauseAdmin authority. Owner-only; emits previous and
    ///         current. The contract's paused state is preserved across
    ///         rotations.
    function setPauseAdmin(address newAdmin) external onlyOwner {
        if (newAdmin == address(0)) revert ZeroAddress();
        address previous = pauseAdmin;
        pauseAdmin = newAdmin;
        emit PauseAdminUpdated(previous, newAdmin);
    }

    function mint(address to, uint256 amount, bytes32 withdrawalId)
        external
        onlyROFL
        whenNotPaused
    {
        if (amount == 0) revert ZeroAmount();
        if (withdrawalId == bytes32(0)) revert ZeroId();
        if (mintedWithdrawalIds[withdrawalId]) revert AlreadyProcessed();
        mintedWithdrawalIds[withdrawalId] = true;
        xrose.mint(to, amount);
        emit Minted(withdrawalId, to, amount);
    }

    /// @notice Burn `amount` of xROSE from the bridge's own balance against `depositId`.
    /// @dev Caller (ROFL) is responsible for ensuring the corresponding sweep
    ///      `transfer(address(this), amount)` from the user's deposit keypair has
    ///      already landed on this chain before invoking burn — this contract
    ///      burns from its own balance via `xrose.burn(address(this), amount)`
    ///      (the `msg.sender == _user` fast path in XRose, no allowance needed).
    ///      Replay protection is per-`depositId` only: the on-chain
    ///      `BridgeBurnRequest.amount` recorded on Sapphire is not echoed here,
    ///      so a ROFL bug calling burn with the wrong amount for a given
    ///      depositId is not caught by this contract — only by the explicit
    ///      sweep-balance check below (which catches "sweep never landed") and
    ///      by xROSE's own `_burn` (which catches over-burn).
    function burn(uint256 amount, bytes32 depositId)
        external
        onlyROFL
        whenNotPaused
    {
        if (amount == 0) revert ZeroAmount();
        if (depositId == bytes32(0)) revert ZeroId();
        if (burnedDepositIds[depositId]) revert AlreadyProcessed();
        uint256 balance = IERC20(address(xrose)).balanceOf(address(this));
        if (balance < amount) revert InsufficientSweep(balance, amount);
        burnedDepositIds[depositId] = true;
        xrose.burn(address(this), amount);
        emit Burned(depositId, amount);
    }

    function pause() external onlyPauseAdmin whenNotPaused {
        _paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external onlyPauseAdmin whenPaused {
        _paused = false;
        emit Unpaused(msg.sender);
    }

    function paused() external view returns (bool) {
        return _paused;
    }
}
