"""HTTP helpers for auth endpoints."""

from typing import Iterable, Optional

from fastapi import HTTPException, Request


def no_store_headers() -> dict[str, str]:
    """Return cache-busting headers for credential-issuing responses."""
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }


def auth_exception(
    status_code: int,
    detail: str,
    headers: Optional[dict[str, str]] = None,
) -> HTTPException:
    """Create an auth-related HTTP exception with cache-busting headers."""
    response_headers = no_store_headers()
    if headers:
        response_headers.update(headers)
    return HTTPException(status_code=status_code, detail=detail, headers=response_headers)


def request_origin(request: Request) -> Optional[str]:
    """Return the normalized Origin header when present."""
    origin = request.headers.get("origin")
    if origin is None:
        return None
    origin = origin.strip().rstrip("/")
    return origin or None


def enforce_expected_origin(
    request: Request,
    *,
    expected_origins: Iterable[str],
    detail: str,
    allow_missing: bool = True,
) -> Optional[str]:
    """Require the request Origin to match one of the expected origins."""
    allowed = {o.rstrip("/") for o in expected_origins if o}
    origin = request_origin(request)
    if origin is None:
        if allow_missing:
            return None
        raise auth_exception(status_code=400, detail=detail)
    if origin not in allowed:
        raise auth_exception(status_code=400, detail=detail)
    return origin
