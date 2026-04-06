"""Authorization flow for cross-domain SIWE authentication."""

import html
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from src.auth.client_registry import ClientRegistry, get_client_registry
from src.auth.code_store import AuthCodeRecord, get_auth_code_store
from src.auth.http import auth_exception, enforce_expected_origin, no_store_headers, request_origin
from src.auth.jwt_service import get_jwt_service
from src.auth.pkce import verify_pkce
from src.auth.rate_limiter import get_auth_rate_limiter, request_identity
from src.auth.siwe_config import get_siwe_config
from src.auth.siwe_service import SiweAuthError, authenticate_siwe_message
from src.config import DEFAULT_SIWE_ALLOWED_CHAIN_IDS, load_settings
from src.models.authorize import (
    AuthorizeCodeRequest,
    AuthorizeCodeResponse,
    HostedAuthorizePageRequest,
    TokenExchangeRequest,
    TokenExchangeResponse,
)

auth_router = APIRouter(prefix="/v1/accounting", tags=["Auth"])

_AUTH_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "auth_authorize.html"
_ERROR_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "auth_error.html"
_AUTH_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "static" / "auth.js"
_AUTH_STYLESHEET_PATH = Path(__file__).resolve().parent.parent / "static" / "auth.css"
_VALID_RESPONSE_MODES = {"web_message", "redirect"}


def _allowed_siwe_chains() -> set[int]:
    settings = load_settings()
    if settings.siwe_allowed_chain_ids:
        return set(settings.siwe_allowed_chain_ids)
    return (
        DEFAULT_SIWE_ALLOWED_CHAIN_IDS
        | set(settings.chain_rpc_urls.keys())
        | {settings.sapphire_chain_id}
    )


def _headers_for_origin(origin: Optional[str]) -> dict[str, str]:
    headers = no_store_headers()
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return headers


def _auth_error(
    status_code: int,
    error: str,
    error_description: str,
    origin: Optional[str] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    headers = _headers_for_origin(origin)
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": error,
            "error_description": error_description,
        },
    )


def _render_error_page(title: str, message: str, status_code: int = 400) -> HTMLResponse:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    template = _ERROR_TEMPLATE_PATH.read_text(encoding="utf-8")
    page = template.replace("__ERROR_TITLE__", safe_title).replace(
        "__ERROR_MESSAGE__", safe_message
    )
    return HTMLResponse(
        content=page,
        status_code=status_code,
        headers={
            **no_store_headers(),
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _rate_limit_retry_after(request: Request, bucket: str, limit: int) -> Optional[int]:
    settings = load_settings()
    return get_auth_rate_limiter().hit(
        bucket=bucket,
        key=request_identity(request),
        limit=limit,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )


def _versioned_asset_url(url: str, asset_file: Path) -> str:
    version = asset_file.stat().st_mtime_ns
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={version}"


def _root_path_aware_path(request: Request, route_name: str, **path_params: str) -> str:
    path = str(request.app.url_path_for(route_name, **path_params))
    root_path = request.scope.get("root_path", "") or ""
    if root_path.endswith("/"):
        root_path = root_path[:-1]
    return f"{root_path}{path}" if root_path else path


@auth_router.get("/auth/authorize", response_class=HTMLResponse)
async def authorize_page(
    request: Request,
    client_id: str = Query(..., min_length=1),
    redirect_uri: str = Query(..., min_length=1),
    code_challenge: str = Query(..., min_length=43, max_length=128),
    state: str = Query(..., min_length=1),
    response_mode: str = Query(default="web_message"),
    code_challenge_method: str = Query(default="S256"),
    chain_id: int = Query(..., ge=1),
) -> HTMLResponse:
    """Serve the Flexvaults auth page for popup/redirect SIWE login."""
    registry = get_client_registry()
    settings = load_settings()
    payload = HostedAuthorizePageRequest(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        response_mode=response_mode,
        code_challenge_method=code_challenge_method,
        chain_id=chain_id,
    )

    try:
        siwe_config = get_siwe_config(settings)
    except ValueError:
        return _render_error_page(
            "Auth Misconfigured",
            "SIWE domain is not configured for this service.",
            status_code=500,
        )

    client = registry.get_client(payload.client_id)
    if client is None:
        return _render_error_page("Unknown Application", "This application is not registered.")
    if not registry.validate_redirect_uri(payload.client_id, payload.redirect_uri):
        return _render_error_page(
            "Invalid Redirect", "The supplied redirect URI is not registered."
        )
    if payload.code_challenge_method != "S256":
        return _render_error_page("Unsupported PKCE", "Only S256 PKCE is supported.")
    if payload.response_mode not in _VALID_RESPONSE_MODES:
        return _render_error_page("Unsupported Response Mode", "Unsupported response_mode.")
    if payload.chain_id not in _allowed_siwe_chains():
        return _render_error_page(
            "Unsupported Chain",
            f"Chain ID {payload.chain_id} is not supported for hosted sign-in.",
        )

    normalized_redirect_uri = ClientRegistry.normalize_redirect_uri(payload.redirect_uri)
    redirect_origin = ClientRegistry.redirect_origin(normalized_redirect_uri)
    context = {
        "clientId": client.client_id,
        "displayName": client.display_name,
        "redirectUri": normalized_redirect_uri,
        "redirectOrigin": redirect_origin,
        "codeChallenge": payload.code_challenge,
        "codeChallengeMethod": payload.code_challenge_method,
        "state": payload.state,
        "responseMode": payload.response_mode,
        "preferredChainId": payload.chain_id,
        "supportedChainIds": sorted(_allowed_siwe_chains()),
        "siweDomain": siwe_config.domain,
        "siweOrigin": siwe_config.origin,
        "authTokenValiditySeconds": settings.auth_token_validity_seconds,
        "nonceEndpoint": _root_path_aware_path(request, "get_siwe_nonce"),
        "authorizeEndpoint": _root_path_aware_path(request, "authorize_code"),
    }

    template = _AUTH_TEMPLATE_PATH.read_text(encoding="utf-8")
    escaped_context = json.dumps(context).replace("</", "<\\/")
    html = (
        template.replace("__AUTH_CONTEXT_JSON__", escaped_context)
        .replace(
            "__AUTH_CSS_HREF__",
            _versioned_asset_url(
                _root_path_aware_path(request, "static", path="auth.css"),
                _AUTH_STYLESHEET_PATH,
            ),
        )
        .replace(
            "__AUTH_JS_SRC__",
            _versioned_asset_url(
                _root_path_aware_path(request, "static", path="auth.js"),
                _AUTH_SCRIPT_PATH,
            ),
        )
    )

    # Firefox blocks extension-injected wallet providers under strict script-src.
    # This page has no authored inline JS, so relaxing only script-src keeps the
    # rest of the CSP strict while allowing wallet injection.
    return HTMLResponse(
        content=html,
        headers={
            **no_store_headers(),
            "Content-Security-Policy": (
                "default-src 'none'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self'; "
                "img-src 'self'; "
                "connect-src 'self'; "
                "base-uri 'none'; "
                "form-action 'none'; "
                "frame-ancestors 'none'"
            ),
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@auth_router.post("/auth/authorize", response_model=AuthorizeCodeResponse)
async def authorize_code(
    payload: AuthorizeCodeRequest,
    request: Request,
    response: Response,
) -> AuthorizeCodeResponse:
    """Verify SIWE on the Flexvaults origin and mint an authorization code."""
    settings = load_settings()
    try:
        siwe_config = get_siwe_config(settings)
    except ValueError as exc:
        raise auth_exception(status_code=500, detail=str(exc)) from exc
    enforce_expected_origin(
        request,
        expected_origin=siwe_config.origin,
        detail="Browser authorization requests must originate from the configured auth origin",
        allow_missing=True,
    )
    retry_after = _rate_limit_retry_after(
        request,
        "auth_authorize",
        settings.auth_authorize_rate_limit,
    )
    if retry_after is not None:
        raise auth_exception(
            status_code=429,
            detail="Too many authorization attempts. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )

    registry = get_client_registry()
    if registry.get_client(payload.client_id) is None:
        raise auth_exception(status_code=400, detail="unknown_client")
    if not registry.validate_redirect_uri(payload.client_id, payload.redirect_uri):
        raise auth_exception(status_code=400, detail="invalid_redirect_uri")
    if payload.code_challenge_method != "S256":
        raise auth_exception(status_code=400, detail="invalid_code_challenge_method")

    try:
        auth_result = authenticate_siwe_message(payload.siwe_message, payload.signature)
    except SiweAuthError as exc:
        raise auth_exception(status_code=exc.status_code, detail=exc.detail) from exc

    code_store = get_auth_code_store()
    normalized_redirect_uri = ClientRegistry.normalize_redirect_uri(payload.redirect_uri)
    code = code_store.create_code(
        AuthCodeRecord(
            user_address=auth_result.address,
            client_id=payload.client_id,
            redirect_uri=normalized_redirect_uri,
            code_challenge=payload.code_challenge,
            code_challenge_method=payload.code_challenge_method,
        )
    )
    response.headers.update(no_store_headers())
    return AuthorizeCodeResponse(code=code)


@auth_router.post("/auth/token", response_model=TokenExchangeResponse)
async def exchange_code(payload: TokenExchangeRequest, request: Request) -> JSONResponse:
    """Exchange an authorization code and PKCE verifier for JWT credentials."""
    settings = load_settings()
    registry = get_client_registry()
    client = registry.get_client(payload.client_id)
    if client is None:
        return _auth_error(400, "invalid_client", "Unknown client_id")
    if payload.grant_type != "authorization_code":
        return _auth_error(400, "unsupported_grant_type", "Only authorization_code is supported")
    if not registry.validate_redirect_uri(payload.client_id, payload.redirect_uri):
        return _auth_error(400, "invalid_redirect_uri", "Redirect URI is not registered")

    normalized_redirect_uri = ClientRegistry.normalize_redirect_uri(payload.redirect_uri)
    expected_origin = ClientRegistry.redirect_origin(normalized_redirect_uri)
    request_origin_value = request_origin(request)
    if request_origin_value and request_origin_value != expected_origin:
        return _auth_error(400, "invalid_origin", "Origin does not match redirect URI")

    retry_after = _rate_limit_retry_after(
        request,
        "auth_token_exchange",
        settings.auth_token_rate_limit,
    )
    if retry_after is not None:
        return _auth_error(
            429,
            "rate_limited",
            "Too many token exchange attempts. Please retry later.",
            origin=request_origin_value if request_origin_value == expected_origin else None,
            extra_headers={"Retry-After": str(retry_after)},
        )

    code_store = get_auth_code_store()
    code_record = code_store.get_code(payload.code)
    if code_record is None:
        return _auth_error(400, "invalid_grant", "Authorization code is invalid or expired")
    if code_record.client_id != payload.client_id:
        return _auth_error(
            400, "invalid_client", "client_id does not match the authorization request"
        )
    if code_record.redirect_uri != normalized_redirect_uri:
        return _auth_error(
            400,
            "invalid_redirect_uri",
            "redirect_uri does not match the authorization request",
            origin=request_origin_value if request_origin_value == expected_origin else None,
        )

    try:
        pkce_ok = verify_pkce(
            payload.code_verifier,
            code_record.code_challenge,
            code_record.code_challenge_method,
        )
    except ValueError as exc:
        return _auth_error(
            400,
            "invalid_grant",
            str(exc),
            origin=request_origin_value if request_origin_value == expected_origin else None,
        )
    if not pkce_ok:
        return _auth_error(
            400,
            "invalid_grant",
            "PKCE verification failed",
            origin=request_origin_value if request_origin_value == expected_origin else None,
        )
    if code_store.consume_code(payload.code) is None:
        return _auth_error(
            400,
            "invalid_grant",
            "Authorization code has already been used",
            origin=request_origin_value if request_origin_value == expected_origin else None,
        )

    jwt_service = get_jwt_service()
    response = TokenExchangeResponse(
        access_token=jwt_service.create_token(code_record.user_address),
        id_token=jwt_service.create_id_token(
            code_record.user_address,
            audience=client.audience,
        ),
        refresh_token=jwt_service.create_refresh_token(code_record.user_address),
        token_type="Bearer",
        expires_in=jwt_service.access_token_expiry_seconds,
        refresh_expires_in=jwt_service.refresh_token_expiry_seconds,
        address=code_record.user_address,
    )
    return JSONResponse(
        status_code=200,
        headers=_headers_for_origin(
            request_origin_value if request_origin_value == expected_origin else None
        ),
        content=response.model_dump(),
    )
