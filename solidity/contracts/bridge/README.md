# Bridge contracts — vendor provenance

This directory holds the vendored xERC20 reference (`XRose.sol`, `IXERC20.sol`)
and the project's `ROFLBridge` adapter. This file pins the upstream commit and
records every fact a reviewer needs to re-verify provenance.

## Upstream

- **Repository**: <https://github.com/defi-wonderland/xERC20>

## Pin

| Field      | Value |
|------------|-------|
| Tag        | [`v1.0.0`](https://github.com/defi-wonderland/xERC20/releases/tag/v1.0.0) |
| Commit     | [`da2afabdeb1bad9ccda2f6eb928cd99e852530be`](https://github.com/defi-wonderland/xERC20/tree/da2afabdeb1bad9ccda2f6eb928cd99e852530be) |
| Tag date   | 2025-03-04 |
| Pinned on  | 2026-05-08 |

### Selection rationale

`v1.0.0` is the only tagged release on the upstream repository at the time of
pinning. It ships with an audit by [Creed](https://www.creed.tech) — the
report PDF (`Connext_xTokens_Audit_-_Creed_-_v0.3.pdf`) is attached to the
[v1.0.0 release page](https://github.com/defi-wonderland/xERC20/releases/tag/v1.0.0).
The audited commit is `v1.0.0` itself. Upstream pragma is
`>=0.8.4 <0.9.0`, which covers this project's `solc 0.8.24`.

If a newer audited release becomes available later, do **not** silently change
the pin: bump the SHA in the table above, regenerate `.upstream-lock`, re-run
the XRose vendoring tests, and bump the deploy manifest's `xerc20Source` field.

## License

- **Effective license: MIT.** Source: the
  [`LICENSE`](https://github.com/defi-wonderland/xERC20/blob/da2afabdeb1bad9ccda2f6eb928cd99e852530be/LICENSE)
  file at the repository root (`The MIT License (MIT) © 2024 Wonder Ltd`),
  reaffirmed in the upstream
  [README §License](https://github.com/defi-wonderland/xERC20/blob/da2afabdeb1bad9ccda2f6eb928cd99e852530be/README.md#license)
  ("The primary license for xERC20 is MIT").
- **Per-file SPDX (upstream)**: both `XERC20.sol` and `IXERC20.sol` carry
  `// SPDX-License-Identifier: UNLICENSED` in their headers. This is a
  Foundry-template leftover that contradicts the repo-level MIT grant.
  Vendored copies retain the upstream `UNLICENSED` header **verbatim** so
  byte-level provenance against `.upstream-lock` is preserved; MIT applies
  via the upstream `LICENSE` file. Flagged here so any future audit reviewer
  sees the discrepancy and the resolution.

## Source files imported

| Upstream path                         | Vendor path                              |
|---------------------------------------|------------------------------------------|
| `solidity/contracts/XERC20.sol`       | `solidity/contracts/bridge/XRose.sol`    |
| `solidity/interfaces/IXERC20.sol`     | `solidity/contracts/bridge/IXERC20.sol`  |

`IXERC20` is vendored **verbatim** (full upstream interface). `ROFLBridge`
consumes only the subset of functions it actually needs; the interface
having additional functions is harmless.

`XRose.sol` is the upstream `XERC20.sol` with three local deltas (see
"Applied local deltas" below); the contract is renamed at the Solidity
level so the deployed artifact name matches the deploy manifest
(`xroseConstructor: ["XRose", "xROSE", "<deployerEOA>"]`).

`solidity/contracts/XERC20Lockbox.sol` is **not** vendored; the lockbox is
left unset/unused.

## Constructor (verbatim from upstream)

From [`solidity/contracts/XERC20.sol@v1.0.0` lines 41–44](https://github.com/defi-wonderland/xERC20/blob/da2afabdeb1bad9ccda2f6eb928cd99e852530be/solidity/contracts/XERC20.sol#L41-L44):

```solidity
constructor(string memory _name, string memory _symbol, address _factory) ERC20(_name, _symbol) ERC20Permit(_name) {
  _transferOwnership(_factory);
  FACTORY = _factory;
}
```

## Owner / factory semantics (verbatim from upstream)

The two-line constructor body, verbatim:

```solidity
_transferOwnership(_factory);
FACTORY = _factory;
```

After construction:
- `owner()` returns `_factory`.
- `FACTORY` (immutable, public) returns `_factory`.

`_factory` is the deployer EOA — the same address for both roles.

## Applied local deltas (in `XRose.sol`)

Three deltas applied at vendor time. Verified by `diff` against upstream at
the pinned commit: exactly these lines differ; everything else is byte-equal
to `solidity/contracts/XERC20.sol@v1.0.0`. Deltas 1 and 2 are required for
the file to compile under this project's OZ 5.4 and flattened bridge
directory layout; delta 3 is the project-side rename.

### 1. Import path (`XRose.sol` line 4)

```diff
-import {IXERC20} from '../interfaces/IXERC20.sol';
+import {IXERC20} from './IXERC20.sol';
```

Reason: both files flatten into `solidity/contracts/bridge/`. No upstream
path of `../interfaces/` exists in the vendored layout.

### 2. OZ v5 `Ownable` constructor argument (`XRose.sol` constructor)

```diff
-constructor(string memory _name, string memory _symbol, address _factory) ERC20(_name, _symbol) ERC20Permit(_name) {
-  _transferOwnership(_factory);
+constructor(string memory _name, string memory _symbol, address _factory) ERC20(_name, _symbol) ERC20Permit(_name) Ownable(_factory) {
   FACTORY = _factory;
 }
```

Reason: OpenZeppelin v5's `Ownable` constructor takes `address initialOwner`
and is non-default (see
[`@openzeppelin/contracts/access/Ownable.sol:38`](https://github.com/OpenZeppelin/openzeppelin-contracts/blob/release-v5.4/contracts/access/Ownable.sol#L38)).
Adding `Ownable(_factory)` to the parent-init list both satisfies the
required argument and sets the owner to `_factory` directly. The
`_transferOwnership(_factory)` call from upstream is dropped because
`Ownable(_factory)` already does the equivalent work; keeping it would emit
a redundant `OwnershipTransferred(_factory, _factory)` event.

Net post-construction state is unchanged from upstream's intent:
`owner() == FACTORY == _factory`.

### 3. Contract rename (`XRose.sol` line 9)

```diff
-contract XERC20 is ERC20, Ownable, IXERC20, ERC20Permit {
+contract XRose is ERC20, Ownable, IXERC20, ERC20Permit {
```

Reason: the deployed artifact is named `XRose`; the bytecode-size checker
(`solidity/scripts/check-bytecode-size.ts:172`) looks up `XRose` by name, and
the deploy manifest records `xroseConstructor: ["XRose", "xROSE", "<deployerEOA>"]`.
The rename is identifier-only; no behavior change.

## Notes

- The deploy manifest re-records this provenance plus the deployed runtime
  hash. The two must agree. If the pin changes, both must update in the same
  commit.
- "Do not silently change the constructor shape." If a future pin changes the
  number or order of constructor parameters, propagate the change to the
  XRose vendoring and its tests.
- `.upstream-lock` (sibling file) records SHA-256 hashes of upstream bytes at
  the pinned commit. The vendoring tests re-fetch upstream at the pin and
  assert the lock still matches before/after vendoring.
