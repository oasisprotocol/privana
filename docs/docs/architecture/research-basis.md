---
id: research-basis
title: "Research Basis"
sidebar_position: 5
description: "Privana's architecture is grounded in peer-reviewed cryptographic research published by Cornell Tech."
---

Privana's architecture is grounded in peer-reviewed cryptographic research published by Cornell Tech.

### Primary Research Paper

**"Liquefaction: Privately Liquefying Blockchain Assets"**  
Austgen, Fábrega, Kelkar, Vilardell, Allen, Babel, Yu, and Juels — Cornell Tech  
arXiv: [2412.02634](https://arxiv.org/abs/2412.02634), December 2024

This paper introduces key encumbrance as a primitive, formalizes the SEAO assumption that conventional wallets rely on, and proves security properties of TEE-based policy governance. The paper demonstrates that TEE-based key encumbrance can break the Single-Entity Address-Ownership assumption, enabling private renting, sharing, and pooling of blockchain assets and privileges. Privana is the Oasis Network's production implementation of the Liquefaction framework.

### Key concepts from the research

#### Key Encumbrance

The core primitive. A private key generated and held within a TEE, governed by programmable access-control policies. Enables automation without custody transfer.

#### SEAO Assumption

Single-Entity Address-Ownership — the assumption that one blockchain address is controlled by a single entity. Key encumbrance breaks this in a controlled, beneficial way.

#### Asset-Time Segmentation

The principle that a given asset is exclusively controlled by one sub-policy at any given time. Prevents deadlock and double-spend between competing policies.

#### Overlay Smart Contracts

Because encumbered keys are managed off-chain (in TEEs), smart-contract-like logic can be applied to chains that have no native smart contract support — Bitcoin, Dogecoin, Litecoin, etc.

### Additional references

The FlexVaults whitepaper cites several additional works relevant to the security model: Kelkar et al. on Complete Knowledge proofs (ACM CCS, 2024), Jean-Louis et al. on privacy flaws in TEE-based platforms (ePrint 2023/378), and Van Schaik et al. on SGX security (IEEE S&P, 2024). For readers interested in the full security analysis, the [Liquefaction paper](https://arxiv.org/abs/2412.02634) is the recommended starting point.

For Oasis-specific technical resources: [Sapphire developer documentation](https://docs.oasis.io/dapp/sapphire/), [ROFL documentation](https://docs.oasis.io/build/rofl/), and the [Oasis Protocol overview](https://oasis.net/).
