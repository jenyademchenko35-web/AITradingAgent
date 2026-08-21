"""Version-controlled immutable provenance for the existing Crypto OOS split.

This is deliberately a source registry rather than a value derived from the
advancing production database.  Changing it requires a reviewed code change.
"""

from __future__ import annotations

from datetime import datetime, timezone

CRYPTO_OOS_CUTOFF_ID = "CRYPTO-OOS-V1"
_CRYPTO_OOS_CUTOFF_TEXT = "2026-08-20T09:12:56.327701+00:00"


def get_frozen_oos_cutoff() -> datetime:
    """Return the fixed original Crypto OOS boundary as an aware UTC datetime."""
    parsed = datetime.fromisoformat(_CRYPTO_OOS_CUTOFF_TEXT)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("Crypto OOS cutoff registry is malformed")
    return parsed.astimezone(timezone.utc)
