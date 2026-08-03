// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.0;

import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/**
 * @title Universal Proposeable Upgradeable Proxy Standard (UPUPS)
 * @notice Extends UUPSUpgradeable with a two-step upgrade process that prevents
 * silent simulated upgrades that could extract confidential contract state.
 * The upgrader must first call `proposeUpgrade` to announce the new
 * implementation, and then call regular `upgradeToAndCall` in a later block.
 *
 * #### Example
 *
 * ```solidity
 * contract MyContract is UPUPSUpgradeable, OwnableUpgradeable {
 *   function initialize(address _owner) public initializer {
 *     __Ownable_init(_owner);
 *   }
 *
 *   // Gate proposeUpgrade with appropriate modifier.
 *   function _authorizeProposeUpgrade() internal onlyOwner { }
 *
 *   // Gate UUPSUpgradeable.authorizeUpgrade with additional acceptProposedUpgrade() modifier.
 *   function _authorizeUpgrade(address newImpl) internal onlyOwner acceptProposedUpgrade(newImpl) { }
 * }
 * ```
 */
abstract contract UPUPSUpgradeable is UUPSUpgradeable {
    /// @custom:storage-location erc7201:oasisprotocol.storage.UPUPSUpgradeable
    struct UPUPSUpgradeableStorage {
        address _newImplementation;
        bytes32 _newImplementationHash;
        uint256 _minBlockNumber;
    }

    // keccak256(abi.encode(uint256(keccak256("oasisprotocol.storage.UPUPSUpgradeable")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant UPUPSUpgradeableStorageLocation =
        0x2ba9668233a4827367587178d890a758d4583dfd9e5c9ef8b58defb278a6ad00;

    function _getUPUPSUpgradeableStorage()
        private
        pure
        returns (UPUPSUpgradeableStorage storage $)
    {
        assembly {
            $.slot := UPUPSUpgradeableStorageLocation
        }
    }

    error ImplementationDoesNotMatch();
    error ImplementationHashDoesNotMatch();
    error MinBlockNumberNotReached();
    error MinBlockNumberInPast();
    error NewImplementationNotAContract();

    event UpgradeProposed(
        address indexed newImplementation,
        bytes32 indexed newImplementationHash,
        uint256 indexed minBlockNumber
    );

    event UpgradeAccepted(
        address indexed newImplementation,
        bytes32 indexed newImplementationHash,
        uint256 indexed minBlockNumber
    );

    /**
     * @notice Initializes the contract.
     */
    function __UPUPSUpgradeable_init()
        internal
        onlyInitializing
    {
    }

    /**
     * @notice Checks if the proposed upgrade is valid and clears the proposed
     * upgrade bits.
     */
    modifier acceptProposedUpgrade(address newImplementation) {
        _acceptProposeUpgrade(newImplementation);

        _;
    }

    /**
     * @notice Function that should revert when `msg.sender` is not authorized
     * to propose the upgrade. Use {proposedUpgradeImplementation} and
     * {proposedUpgradeMinBlockNumber} to fetch current proposal details. Called
     * by {proposeUpgrade}.
     *
     * Normally, this function will use an xref:access.adoc[access control]
     * modifier such as {Ownable-onlyOwner}.
     *
     * ```solidity
     * function _authorizeProposeUpgrade() internal onlyOwner {}
     * ```
     */
    function _authorizeProposeUpgrade() internal virtual;

    /**
     * @notice Returns the new implementation address of the proposed upgrade.
     */
    function proposedUpgradeImplementation() public view virtual returns (address) {
        UPUPSUpgradeableStorage storage $ = _getUPUPSUpgradeableStorage();
        return $._newImplementation;
    }

    /**
     * @notice Returns the hash of the implementation runtime code of the proposed upgrade.
     */
    function proposedUpgradeImplementationHash() public view virtual returns (bytes32) {
        UPUPSUpgradeableStorage storage $ = _getUPUPSUpgradeableStorage();
        return $._newImplementationHash;
    }

    /**
     * @notice Returns the proposed upgrade minimum block number.
     */
    function proposedUpgradeMinBlockNumber() public view virtual returns (uint256) {
        UPUPSUpgradeableStorage storage $ = _getUPUPSUpgradeableStorage();
        return $._minBlockNumber;
    }

    /**
     * @notice Reverts unless the implementations matches and the current block
     * number is at least the minBlockNumber.
     */
    function _acceptProposeUpgrade(address newImplementation) internal virtual {
        if (newImplementation != proposedUpgradeImplementation()) {
            revert ImplementationDoesNotMatch();
        }
        if (newImplementation.codehash != proposedUpgradeImplementationHash()) {
            revert ImplementationHashDoesNotMatch();
        }
        if (block.number < proposedUpgradeMinBlockNumber()) {
            revert MinBlockNumberNotReached();
        }
        emit UpgradeAccepted(newImplementation, newImplementation.codehash, proposedUpgradeMinBlockNumber());

        UPUPSUpgradeableStorage storage $ = _getUPUPSUpgradeableStorage();
        $._newImplementation = address(0);
        $._minBlockNumber = 0;
    }

    /**
     * @notice Propose the upgrade to the new implementation address after the
     * given block number. If minBlockNumber is zero, take the number of the
     * next block.
     * @dev minBlockNumber is intentionally left to the caller's discretion:
     * the function guards against the simulated upgrade attack, but a longer
     * window may be provided for the new contract implementation review.
     */
    function proposeUpgrade(address newImplementation, uint256 minBlockNumber) public virtual {
        if (newImplementation.code.length == 0) {
            revert NewImplementationNotAContract();
        }
        if (minBlockNumber == 0) {
            minBlockNumber = block.number+1;
        }
        if (minBlockNumber <= block.number) {
            revert MinBlockNumberInPast();
        }

        UPUPSUpgradeableStorage storage $ = _getUPUPSUpgradeableStorage();
        $._newImplementation = newImplementation;
        $._newImplementationHash = newImplementation.codehash;
        $._minBlockNumber = minBlockNumber;

        _authorizeProposeUpgrade();

        emit UpgradeProposed(newImplementation, newImplementation.codehash, minBlockNumber);
    }
}
