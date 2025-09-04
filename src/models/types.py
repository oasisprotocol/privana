"""Type definitions for the Accounting Module API."""

from dataclasses import dataclass


@dataclass
class Settings:
    """Application configuration settings."""
    
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    environment: str = "development"
