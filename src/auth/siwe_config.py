"""Canonical SIWE domain/origin parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from src.models.types import Settings

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class SiweConfig:
    """Normalized SIWE configuration."""

    domain: str
    origin: str


def is_loopback_host(host: str | None) -> bool:
    """Return True when the host is a loopback development host."""
    return bool(host) and host.lower() in _LOOPBACK_HOSTS


def _canonical_netloc(scheme: str, host: str | None, port: int | None) -> str:
    if not host:
        raise ValueError("Origin must include a host")

    normalized_host = host.lower()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"

    default_port = _DEFAULT_PORTS.get(scheme.lower())
    if port is None or port == default_port:
        return normalized_host
    return f"{normalized_host}:{port}"


def normalize_redirect_uri(redirect_uri: str) -> str:
    """Normalize a redirect URI while preserving its path and query string."""
    parsed = urlsplit(redirect_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError(f"Invalid redirect_uri: {redirect_uri}")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"Invalid redirect_uri: {redirect_uri}")
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
        raise ValueError(
            "redirect_uri must use https unless it targets localhost/loopback development"
        )

    return urlunsplit(
        (
            parsed.scheme.lower(),
            _canonical_netloc(parsed.scheme, parsed.hostname, parsed.port),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def get_siwe_config(settings: Settings) -> SiweConfig:
    """Normalize SIWE_DOMAIN into a SIWE domain and browser origin."""
    raw_value = (settings.siwe_domain or "").strip()
    if not raw_value:
        raise ValueError("SIWE_DOMAIN not configured")

    if "://" in raw_value:
        parsed = urlsplit(raw_value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("SIWE_DOMAIN must use http or https when a scheme is provided")
        if not parsed.netloc or not parsed.hostname:
            raise ValueError("SIWE_DOMAIN must include a host")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("SIWE_DOMAIN must not include a path, query, or fragment")
        if parsed.username or parsed.password:
            raise ValueError("SIWE_DOMAIN must not include userinfo")
        if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
            raise ValueError("SIWE_DOMAIN may use http only for localhost/loopback development")
        netloc = _canonical_netloc(parsed.scheme, parsed.hostname, parsed.port)
        return SiweConfig(
            domain=netloc,
            origin=f"{parsed.scheme}://{netloc}",
        )

    parsed = urlsplit(f"//{raw_value}")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("SIWE_DOMAIN must be a bare host[:port] or origin")
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("SIWE_DOMAIN must not include a path, query, fragment, or userinfo")

    environment = str(getattr(settings, "environment", "production")).lower()
    scheme = (
        "http" if environment == "development" and is_loopback_host(parsed.hostname) else "https"
    )
    netloc = _canonical_netloc(scheme, parsed.hostname, parsed.port)
    return SiweConfig(
        domain=netloc,
        origin=f"{scheme}://{netloc}",
    )
