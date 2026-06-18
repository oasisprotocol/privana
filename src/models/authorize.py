"""Authorization request and response models."""

from pydantic import BaseModel, Field


class AuthorizeCodeRequest(BaseModel):
    """Backend authorization-code request sent by the auth page."""

    siwe_message: str = Field(..., min_length=1)
    signature: str = Field(..., min_length=4)
    client_id: str = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1)
    code_challenge: str = Field(..., min_length=43, max_length=128)
    code_challenge_method: str = Field(default="S256", min_length=1)


class AuthorizeCodeResponse(BaseModel):
    """Authorization-code response returned to the auth page."""

    code: str = Field(..., description="Short-lived single-use authorization code")


class TokenExchangeRequest(BaseModel):
    """Authorization-code exchange request."""

    grant_type: str = Field(default="authorization_code", min_length=1)
    code: str = Field(..., min_length=1)
    code_verifier: str = Field(..., min_length=43, max_length=128)
    client_id: str = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1)


class HostedAuthorizePageRequest(BaseModel):
    """Query parameters required to render the hosted auth page."""

    client_id: str = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1)
    code_challenge: str = Field(..., min_length=43, max_length=128)
    state: str = Field(..., min_length=1)
    response_mode: str = Field(default="web_message", min_length=1)
    code_challenge_method: str = Field(default="S256", min_length=1)


class TokenExchangeResponse(BaseModel):
    """Token response returned to third-party clients."""

    access_token: str = Field(..., description="JWT access token for Privana API calls")
    id_token: str = Field(
        ..., description="Client-scoped identity token for third-party backend verification"
    )
    refresh_token: str = Field(..., description="JWT refresh token")
    token_type: str = Field(default="Bearer")
    expires_in: int = Field(..., description="Access token expiry in seconds")
    refresh_expires_in: int = Field(..., description="Refresh token expiry in seconds")
    address: str = Field(..., description="Authenticated Ethereum address")
