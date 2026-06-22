"""Shared value type for authenticated private reads."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PrivateReadAuth:
    """Authenticated SIWE token plus the resolved user address.

    Passed to service methods that need both the token (for the confidential
    contract read) and the resolved address (echoed in the response or used for
    validation). Methods that need only one credential keep the primitive:
    token-only reads take ``siwe_token: bytes`` (e.g. get_deposit_address,
    get_history), and address-only public reads take ``user_address: str``
    (e.g. the nonce getters).
    """

    token: bytes
    user_address: str
