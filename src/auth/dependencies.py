"""FastAPI dependencies for JWT-based authentication."""

import logging
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.jwt_service import get_jwt_service

logger = logging.getLogger(__name__)

# HTTPBearer extracts "Bearer <token>" from Authorization header
# auto_error=False allows optional authentication
_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentAccessToken:
    address: str
    expires_at: int


def _get_access_token_payload(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jwt_service = get_jwt_service()

    try:
        return jwt_service.get_access_token_payload(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid token presented: {e}")
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except ValueError as e:
        raise HTTPException(
            status_code=401,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency to get the current authenticated user.

    Extracts and verifies the JWT from the Authorization header.

    Returns:
        Checksummed Ethereum address of the authenticated user.

    Raises:
        HTTPException: 401 if not authenticated or token invalid.
    """
    return str(_get_access_token_payload(credentials)["sub"])


def get_current_access_token_without_siwe_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> CurrentAccessToken:
    if request.headers.get("Authorization") and request.headers.get("X-SIWE-Token"):
        raise HTTPException(
            status_code=400,
            detail="Provide Authorization bearer token only; do not send X-SIWE-Token",
        )
    payload = _get_access_token_payload(credentials)
    try:
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401,
            detail="Token missing 'exp' claim",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return CurrentAccessToken(address=str(payload["sub"]), expires_at=expires_at)


def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[str]:
    """FastAPI dependency to optionally get the current authenticated user.

    Similar to get_current_user but returns None instead of raising
    an exception if not authenticated.

    Returns:
        Checksummed Ethereum address if authenticated, None otherwise.
    """
    if not credentials:
        return None

    jwt_service = get_jwt_service()

    try:
        address = jwt_service.get_address_from_token(credentials.credentials)
        return address
    except (jwt.InvalidTokenError, ValueError):
        # Expected errors for invalid/expired tokens
        return None
    except Exception:
        logger.warning("Unexpected error during optional token verification", exc_info=True)
        return None
