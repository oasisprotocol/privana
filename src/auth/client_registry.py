"""Static auth client registry with exact redirect URI validation."""

import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from src.auth.siwe_config import normalize_redirect_uri as normalize_auth_redirect_uri
from src.config import load_settings


@dataclass(frozen=True)
class AuthClient:
    """Registered auth client."""

    client_id: str
    display_name: str
    audience: str
    redirect_uris: tuple[str, ...]


class ClientRegistry:
    """Registry of statically configured auth clients."""

    def __init__(self, clients_json: str) -> None:
        try:
            raw_clients = json.loads(clients_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid AUTH_CLIENTS JSON: {exc}") from exc

        if not isinstance(raw_clients, list):
            raise ValueError("AUTH_CLIENTS must be a JSON array")

        clients: dict[str, AuthClient] = {}
        for raw in raw_clients:
            if not isinstance(raw, dict):
                raise ValueError("Each AUTH_CLIENTS entry must be an object")
            client_id = str(raw.get("client_id", "")).strip()
            display_name = str(raw.get("display_name", client_id)).strip()
            audience = str(raw.get("audience", client_id)).strip()
            redirect_uris = tuple(
                self.normalize_redirect_uri(str(uri).strip())
                for uri in raw.get("redirect_uris", [])
            )
            if not client_id:
                raise ValueError("Auth client_id must not be empty")
            if not audience:
                raise ValueError(f"Auth client {client_id} must define a non-empty audience")
            if not redirect_uris:
                raise ValueError(f"Auth client {client_id} must define redirect_uris")
            clients[client_id] = AuthClient(
                client_id=client_id,
                display_name=display_name or client_id,
                audience=audience,
                redirect_uris=redirect_uris,
            )

        self._clients = clients

    def get_client(self, client_id: str) -> Optional[AuthClient]:
        """Return a registered client or None."""
        return self._clients.get(client_id)

    def require_client(self, client_id: str) -> AuthClient:
        """Return a registered client or raise a ValueError."""
        client = self.get_client(client_id)
        if client is None:
            raise ValueError(f"Unknown auth client_id: {client_id}")
        return client

    def validate_redirect_uri(self, client_id: str, redirect_uri: str) -> bool:
        """Return True when the exact redirect URI is registered for the client."""
        client = self.get_client(client_id)
        if client is None:
            return False
        try:
            normalized_redirect_uri = self.normalize_redirect_uri(redirect_uri)
        except ValueError:
            return False
        return normalized_redirect_uri in client.redirect_uris

    @staticmethod
    def normalize_redirect_uri(redirect_uri: str) -> str:
        """Normalize a redirect URI before registry comparisons."""
        return normalize_auth_redirect_uri(redirect_uri)

    @staticmethod
    def redirect_origin(redirect_uri: str) -> str:
        """Extract and validate the origin from a redirect URI."""
        normalized_redirect_uri = ClientRegistry.normalize_redirect_uri(redirect_uri)
        parsed = urlsplit(normalized_redirect_uri)
        return f"{parsed.scheme}://{parsed.netloc}"

    def allowed_origins(self) -> list[str]:
        """Return the deduplicated set of registered client origins."""
        origins = {
            self.redirect_origin(redirect_uri)
            for client in self._clients.values()
            for redirect_uri in client.redirect_uris
        }
        return sorted(origins)


_client_registry_instance: Optional[ClientRegistry] = None


def get_client_registry() -> ClientRegistry:
    """Return the process-wide auth client registry singleton."""
    global _client_registry_instance
    if _client_registry_instance is None:
        settings = load_settings()
        _client_registry_instance = ClientRegistry(settings.auth_clients_json)
    return _client_registry_instance
