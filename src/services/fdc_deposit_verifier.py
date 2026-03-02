"""FDC deposit verification service.

Orchestrates the Flare FDC attestation flow:
1. Prepare attestation request via Verifier API
2. Submit to FdcHub on Coston2 (pays C2FLR fee)
3. Wait for voting round completion (~90s)
4. Retrieve proof from DA layer
5. Verify on Coston2 (free view call)
6. Return verified deposit data
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from src.abi.fdc_contracts import FDC_FEE_CONFIG_ABI, FDC_HUB_ABI, FDC_VERIFICATION_ABI
from src.config.fdc_config import FDCConfig

logger = logging.getLogger(__name__)


class FDCVerificationError(Exception):
    """Raised when FDC verification fails."""


@dataclass
class FDCVerifiedDeposit:
    """Verified deposit data extracted from FDC attestation response."""

    tx_hash: str
    chain_id: int
    source_address: str
    receiving_address: str
    value: int
    status: int
    events: list


def _to_bytes32_string(s: str) -> str:
    """Convert a short string to a 0x-prefixed bytes32 hex (right-padded with zeros)."""
    hex_str = s.encode().hex()
    if len(hex_str) > 64:
        raise ValueError(f"String too long for bytes32: {s}")
    return "0x" + hex_str.ljust(64, "0")


_ZERO_ADDRESS = "0x" + "0" * 40
_EVM_TRANSACTION_BYTES32 = _to_bytes32_string("EVMTransaction")


class FDCDepositVerifier:
    """Orchestrates the full FDC attestation and verification flow."""

    def __init__(self, config: FDCConfig):
        self._config = config
        self._http_client: httpx.AsyncClient | None = None
        self._submit_lock = asyncio.Lock()

        # Coston2 web3 for contract calls
        self._coston2_web3 = Web3(Web3.HTTPProvider(config.coston2_rpc_url))
        self._coston2_web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._coston2_account: Account = Account.from_key(config.coston2_private_key)

        # FDC contracts on Coston2
        self._fdc_hub = self._coston2_web3.eth.contract(
            address=Web3.to_checksum_address(config.hub_address),
            abi=FDC_HUB_ABI,
        )
        self._fdc_fee_config = self._coston2_web3.eth.contract(
            address=Web3.to_checksum_address(config.fee_config_address),
            abi=FDC_FEE_CONFIG_ABI,
        )
        self._fdc_verification = self._coston2_web3.eth.contract(
            address=Web3.to_checksum_address(config.verification_address),
            abi=FDC_VERIFICATION_ABI,
        )

    def _get_http_client(self) -> httpx.AsyncClient:
        """Lazily create a shared httpx client for API calls."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=30,
                headers={
                    "Content-Type": "application/json",
                    "X-API-KEY": self._config.api_key,
                },
            )
        return self._http_client

    async def close(self) -> None:
        """Close the shared HTTP client."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def verify_deposit(self, chain_id: int, tx_hash: str) -> FDCVerifiedDeposit:
        """Full FDC verification flow. Returns verified deposit data."""
        source_id = self._config.source_ids.get(chain_id)
        if source_id is None:
            raise FDCVerificationError(f"Unsupported chain ID for FDC: {chain_id}")

        # 1. Prepare attestation request
        encoded_request = await self._prepare_request(tx_hash, source_id)

        # 2. Submit to FdcHub on Coston2
        voting_round_id = await self._submit_request(encoded_request)

        # 3. Wait for proof from DA layer
        proof_data = await self._wait_for_proof(voting_round_id, encoded_request)

        # 4. Verify on Coston2 (free view call)
        is_valid = await self._verify_on_coston2(proof_data)
        if not is_valid:
            raise FDCVerificationError(f"FDC on-chain verification failed for tx {tx_hash}")

        # 5. Extract verified deposit data
        response_body = proof_data["response"]["responseBody"]
        return FDCVerifiedDeposit(
            tx_hash=tx_hash,
            chain_id=chain_id,
            source_address=response_body["sourceAddress"],
            receiving_address=response_body.get("receivingAddress", _ZERO_ADDRESS),
            value=int(response_body["value"]),
            status=int(response_body["status"]),
            events=response_body.get("events", []),
        )

    async def _prepare_request(self, tx_hash: str, source_id: str) -> str:
        """Call Verifier API to get ABI-encoded attestation request."""
        attestation_type = _EVM_TRANSACTION_BYTES32
        source_id_bytes32 = _to_bytes32_string(source_id)

        url = f"{self._config.verifier_base_url}verifier/eth/EVMTransaction/prepareRequest"
        payload = {
            "attestationType": attestation_type,
            "sourceId": source_id_bytes32,
            "requestBody": {
                "transactionHash": tx_hash,
                "requiredConfirmations": str(self._config.required_confirmations),
                "provideInput": True,
                "listEvents": True,
                "logIndices": [],
            },
        }

        client = self._get_http_client()
        response = await client.post(url, json=payload)

        if response.status_code != 200:
            raise FDCVerificationError(
                f"Verifier API error ({response.status_code}): {response.text}"
            )

        data = response.json()
        if data.get("status") == "INVALID" or not data.get("abiEncodedRequest"):
            raise FDCVerificationError(f"Verifier rejected request: {data}")

        logger.info("Attestation request prepared for tx %s", tx_hash)
        return data["abiEncodedRequest"]

    async def _submit_request(self, encoded_request: str) -> int:
        """Submit attestation request to FdcHub on Coston2. Returns voting round ID.

        Serialized with an asyncio.Lock to prevent concurrent nonce reads
        from producing duplicate nonces when multiple deposits are processed
        at the same time.
        """
        async with self._submit_lock:
            return await self._do_submit_in_thread(encoded_request)

    async def _do_submit_in_thread(self, encoded_request: str) -> int:

        def _do_submit() -> int:
            request_bytes = Web3.to_bytes(hexstr=encoded_request)

            # Get fee
            try:
                fee = self._fdc_fee_config.functions.getRequestFee(request_bytes).call()
                logger.info("Attestation fee: %s C2FLR", Web3.from_wei(fee, "ether"))
            except Exception as exc:
                fee = Web3.to_wei(0.5, "ether")
                logger.warning("Fee lookup failed (%s), using default: 0.5 C2FLR", exc)

            # Build and sign transaction
            tx = self._fdc_hub.functions.requestAttestation(request_bytes).build_transaction(
                {
                    "from": self._coston2_account.address,
                    "value": fee,
                    "nonce": self._coston2_web3.eth.get_transaction_count(
                        self._coston2_account.address
                    ),
                    "gas": 500_000,
                    "gasPrice": self._coston2_web3.eth.gas_price,
                }
            )
            signed = self._coston2_account.sign_transaction(tx)
            tx_hash = self._coston2_web3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._coston2_web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt["status"] != 1:
                raise FDCVerificationError(f"FdcHub requestAttestation reverted: {tx_hash.hex()}")

            # Calculate voting round ID
            block = self._coston2_web3.eth.get_block(receipt["blockNumber"])
            voting_round_id = (
                block["timestamp"] - self._config.first_voting_round_start_ts
            ) // self._config.voting_epoch_duration_s

            logger.info(
                "Attestation submitted: coston2_tx=%s voting_round=%d",
                tx_hash.hex(),
                voting_round_id,
            )
            return voting_round_id

        return await asyncio.to_thread(_do_submit)

    async def _wait_for_proof(self, voting_round_id: int, encoded_request: str) -> dict:
        """Poll DA layer for proof. Waits initial delay then polls with backoff."""
        url = f"{self._config.da_layer_url}api/v0/fdc/get-proof-round-id-bytes"
        start = time.monotonic()
        delay_s = 10.0
        attempts = 0

        # Wait for voting round to complete
        logger.info(
            "Waiting %ds for voting round %d to complete...",
            self._config.initial_proof_delay_s,
            voting_round_id,
        )
        await asyncio.sleep(self._config.initial_proof_delay_s)

        while True:
            attempts += 1
            elapsed = time.monotonic() - start

            if elapsed > self._config.max_wait_s:
                raise FDCVerificationError(
                    f"Timed out after {elapsed:.0f}s waiting for proof "
                    f"(voting round {voting_round_id})"
                )

            try:
                client = self._get_http_client()
                response = await client.post(
                    url,
                    json={
                        "votingRoundId": voting_round_id,
                        "requestBytes": encoded_request,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    if data.get("proof") and data.get("response"):
                        logger.info(
                            "Proof available after %.0fs (%d attempts)",
                            elapsed,
                            attempts,
                        )
                        return data

                logger.info(
                    "[%.0fs] Attempt %d: proof not yet available (status %d), retrying in %.0fs...",
                    elapsed,
                    attempts,
                    response.status_code,
                    delay_s,
                )
            except httpx.RequestError as exc:
                logger.warning(
                    "[%.0fs] Attempt %d: %s, retrying in %.0fs...",
                    elapsed,
                    attempts,
                    exc,
                    delay_s,
                )

            await asyncio.sleep(delay_s)
            delay_s = min(delay_s * 1.5, 30.0)

    async def _verify_on_coston2(self, proof_data: dict) -> bool:
        """Verify FDC proof via view call on Coston2. Free, no gas."""
        resp = proof_data["response"]
        body = resp["responseBody"]

        proof_struct = {
            "merkleProof": proof_data["proof"],
            "data": {
                "attestationType": resp["attestationType"],
                "sourceId": resp["sourceId"],
                "votingRound": int(resp["votingRound"]),
                "lowestUsedTimestamp": int(resp["lowestUsedTimestamp"]),
                "requestBody": {
                    "transactionHash": resp["requestBody"]["transactionHash"],
                    "requiredConfirmations": int(resp["requestBody"]["requiredConfirmations"]),
                    "provideInput": resp["requestBody"]["provideInput"],
                    "listEvents": resp["requestBody"]["listEvents"],
                    "logIndices": [int(i) for i in resp["requestBody"].get("logIndices", [])],
                },
                "responseBody": {
                    "blockNumber": int(body["blockNumber"]),
                    "timestamp": int(body["timestamp"]),
                    "sourceAddress": Web3.to_checksum_address(body["sourceAddress"]),
                    "isDeployment": body.get("isDeployment", False),
                    "receivingAddress": Web3.to_checksum_address(
                        body.get("receivingAddress", _ZERO_ADDRESS)
                    ),
                    "value": int(body["value"]),
                    "input": body.get("input", "0x"),
                    "status": int(body["status"]),
                    "events": [
                        {
                            "logIndex": int(e["logIndex"]),
                            "emitterAddress": Web3.to_checksum_address(e["emitterAddress"]),
                            "topics": e["topics"],
                            "data": e.get("data", "0x"),
                            "removed": e.get("removed", False),
                        }
                        for e in body.get("events", [])
                    ],
                },
            },
        }

        def _do_verify() -> bool:
            return self._fdc_verification.functions.verifyEVMTransaction(proof_struct).call()

        is_valid = await asyncio.to_thread(_do_verify)
        logger.info("FDC on-chain verification result: %s", is_valid)
        return is_valid
