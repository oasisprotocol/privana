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


def _parse_siwe_domain(raw_value: str, settings: Settings) -> SiweConfig:
    """Normalize a single SIWE domain entry into a SiweConfig."""
    raw_value = raw_value.strip()
    if not raw_value:
        raise ValueError("SIWE domain entry must not be empty")

    if "://" in raw_value:
        parsed = urlsplit(raw_value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                "SIWE_DOMAINS entries must use http or https when a scheme is provided"
            )
        if not parsed.netloc or not parsed.hostname:
            raise ValueError("SIWE_DOMAINS entries must include a host")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("SIWE_DOMAINS entries must not include a path, query, or fragment")
        if parsed.username or parsed.password:
            raise ValueError("SIWE_DOMAINS entries must not include userinfo")
        if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
            raise ValueError(
                "SIWE_DOMAINS entries may use http only for localhost/loopback development"
            )
        netloc = _canonical_netloc(parsed.scheme, parsed.hostname, parsed.port)
        return SiweConfig(
            domain=netloc,
            origin=f"{parsed.scheme}://{netloc}",
        )

    parsed = urlsplit(f"//{raw_value}")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("SIWE_DOMAINS entries must be a bare host[:port] or origin")
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(
            "SIWE_DOMAINS entries must not include a path, query, fragment, or userinfo"
        )

    environment = str(getattr(settings, "environment", "production")).lower()
    scheme = (
        "http" if environment == "development" and is_loopback_host(parsed.hostname) else "https"
    )
    netloc = _canonical_netloc(scheme, parsed.hostname, parsed.port)
    return SiweConfig(
        domain=netloc,
        origin=f"{scheme}://{netloc}",
    )


def get_siwe_configs(settings: Settings) -> tuple[SiweConfig, ...]:
    """Normalize all configured SIWE domains into SiweConfig entries.

    Raises ``ValueError`` when no domains are configured. Duplicate entries
    (after canonicalization) are de-duplicated while preserving order.
    """
    raw_entries = tuple(getattr(settings, "siwe_domains", ()) or ())
    if not raw_entries:
        raise ValueError("SIWE_DOMAINS not configured")

    configs: list[SiweConfig] = []
    seen_domains: set[str] = set()
    for raw in raw_entries:
        cfg = _parse_siwe_domain(raw, settings)
        if cfg.domain in seen_domains:
            continue
        seen_domains.add(cfg.domain)
        configs.append(cfg)

    if not configs:
        raise ValueError("SIWE_DOMAINS not configured")
    return tuple(configs)


def get_siwe_config(settings: Settings) -> SiweConfig:
    """Return the primary (first) configured SIWE config.

    Used by callers that operate on a single default domain (hosted auth page,
    server-minted private-read tokens). Domain allow-list checks must iterate
    :func:`get_siwe_configs` instead.
    """
    return get_siwe_configs(settings)[0]
