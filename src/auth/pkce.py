"""PKCE helpers for authorization-code exchange."""

import base64
import hashlib


def verify_pkce(code_verifier: str, code_challenge: str, code_challenge_method: str) -> bool:
    """Validate a PKCE verifier/challenge pair."""
    if code_challenge_method != "S256":
        raise ValueError(f"Unsupported code_challenge_method: {code_challenge_method}")

    try:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    except UnicodeEncodeError as exc:
        raise ValueError("PKCE code_verifier must contain only ASCII characters") from exc
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return expected == code_challenge
