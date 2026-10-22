"""MCP server for eBiblio Catalunya (Odilo): newspapers, magazines and loans."""

from .client import Checkout, OdiloClient, OdiloError, Record, normalise_title
from .config import ConfigError, Settings

__all__ = [
    "Checkout",
    "ConfigError",
    "OdiloClient",
    "OdiloError",
    "Record",
    "Settings",
    "normalise_title",
]

__version__ = "0.1.3"
