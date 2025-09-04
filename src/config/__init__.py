"""Configuration management for the Accounting Module API."""

import logging

from src.models.types import Settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


def load_settings() -> Settings:
    """
    Load settings.
    
    Returns:
        Configured Settings object
    """
    return Settings(
        api_host="0.0.0.0",
        api_port=8000,
        log_level="INFO",
        environment="development"
    )


__all__ = ["load_settings"]
