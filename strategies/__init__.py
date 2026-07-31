"""Auto-discovered strategy definitions for the shadow research laboratory."""

from .registry import StrategyDefinition, StrategyRegistry, registry

# Importing builtins performs registration through the public decorator.
from . import builtins as _builtins  # noqa: F401

__all__ = ["StrategyDefinition", "StrategyRegistry", "registry"]
