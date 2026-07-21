# Security

At [Oasis Foundation], we take security very seriously and we deeply appreciate
any effort to discover and fix vulnerabilities in this project and other
projects powering the [Oasis Network].

We prefer that security reports be sent through our [private bug bounty program
linked on our website](https://oasis.net/security-and-tees).

[Oasis Foundation]: https://oasis.io/
[Oasis Network]: https://docs.oasis.io/general/oasis-network/

## Scope

Security-relevant components of this repository include:

- The Solidity contracts under [`solidity/contracts/`](solidity/contracts/) —
  in particular fund accounting, deposit verification, withdrawal signing, and
  SIWE-based authentication.
- The Python service under [`src/`](src/) that runs inside a ROFL TEE —
  deposit verification, sweep state machine, withdrawal resolution, and the
  HTTP API (JWT/SIWE auth, MoonPay on-ramp endpoints).

## Reporting a vulnerability

Please do **not** open a public GitHub issue for suspected vulnerabilities.
Use the bug bounty program linked above, or report privately via
[GitHub security advisories](../../security/advisories/new) for this
repository.

When reporting, please include:

- A description of the issue and its potential impact.
- Steps to reproduce, ideally with a proof of concept.
- The affected component and commit/version.

We will acknowledge your report, keep you informed of progress, and credit you
in the fix release unless you prefer to remain anonymous.
