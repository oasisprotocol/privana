// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

library SliceBytes {
    /**
     * @notice Extracts a slice from bytes data
     * @param data The bytes data to slice
     * @param begin The starting index (inclusive)
     * @param end The ending index (exclusive)
     * @return result The sliced bytes
     */
    function getSlice(
        bytes memory data,
        uint256 begin,
        uint256 end
    ) internal pure returns (bytes memory result) {
        require(begin <= end, "Invalid slice range");
        require(end <= data.length, "Slice out of bounds");

        result = new bytes(end - begin);
        for (uint256 i = 0; i < end - begin; i++) {
            result[i] = data[i + begin];
        }
    }
}
