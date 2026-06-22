from __future__ import annotations

from typing import Sequence

from .config import ScopedKeyConfig


def validate_scoped_keys_present(scoped_keys: Sequence[ScopedKeyConfig]) -> Sequence[ScopedKeyConfig]:
    """Require explicit scoped authentication keys.

    The bridge no longer supports an unscoped/general API key fallback, so every
    runtime config must define at least one scoped key.
    """
    if not scoped_keys:
        raise ValueError("auth.scoped_keys must define at least one scoped key")
    return scoped_keys
