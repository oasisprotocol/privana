"""Shared value type for authenticated private reads."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PrivateReadAuth:
    """Authenticated SIWE token plus the resolved user address.

    Passed to flows that need both credentials, such as ownership-sensitive
    writes or deposit correlation. Pure private reads pass only the SIWE token
    to the service layer; routes keep the resolved address for response echoes.
    Methods that need only one credential keep the primitive.
    """

    token: bytes
    user_address: str
