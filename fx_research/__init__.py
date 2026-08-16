"""Isolated, shadow-only Forex research foundation.

This package is deliberately not imported by the crypto agent or Research Lab
v2 runtime.  It contains no broker adapter and defaults to disabled.
"""

from .config import FXResearchSettings, get_settings

__all__ = ("FXResearchSettings", "get_settings")
