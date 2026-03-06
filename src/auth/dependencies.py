"""FastAPI dependencies for JWT-based authentication."""

import logging
from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.jwt_service import get_jwt_service

logger = logging.getLogger(__name__)

# HTTPBearer extracts "Bearer <token>" from Authorization header
# auto_error=False allows optional authentication
_bearer_scheme = HTTPBearer(auto_error=False)


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
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jwt_service = get_jwt_service()

    try:
        address = jwt_service.get_address_from_token(credentials.credentials)
        return address
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
