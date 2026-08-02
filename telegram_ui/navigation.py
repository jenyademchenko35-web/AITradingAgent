"""Small in-memory navigation state with TTL expiration."""

from __future__ import annotations

from dataclasses import replace
from threading import RLock
import time
from typing import Callable, Hashable

from .models import NavigationContext


class NavigationStore:
    def __init__(self, ttl_seconds: float = 1800, clock: Callable[[], float] = time.monotonic):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock
        self._values: dict[Hashable, tuple[float, NavigationContext]] = {}
        self._lock = RLock()

    def get(self, key: Hashable) -> NavigationContext:
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return NavigationContext()
            touched_at, state = item
            if self.clock() - touched_at >= self.ttl_seconds:
                self._values.pop(key, None)
                return NavigationContext()
            return state

    def update(
        self,
        key: Hashable,
        *,
        screen: str,
        selected_symbol: str | None = None,
        selected_timeframe: str | None = None,
        page: int = 0,
        last_message_id: int | None = None,
    ) -> NavigationContext:
        if page < 0:
            raise ValueError("page must be non-negative")
        with self._lock:
            old = self.get(key)
            state = NavigationContext(
                current_screen=screen,
                previous_screen=(old.current_screen if old.current_screen != screen else old.previous_screen),
                selected_symbol=selected_symbol if selected_symbol is not None else old.selected_symbol,
                selected_timeframe=(
                    selected_timeframe if selected_timeframe is not None else old.selected_timeframe
                ),
                page=page,
                last_message_id=last_message_id if last_message_id is not None else old.last_message_id,
            )
            self._values[key] = (self.clock(), state)
            return state

    def back(self, key: Hashable) -> NavigationContext:
        with self._lock:
            old = self.get(key)
            target = old.previous_screen or "home"
            state = replace(old, current_screen=target, previous_screen="home", page=0)
            self._values[key] = (self.clock(), state)
            return state

    def clear(self, key: Hashable) -> None:
        with self._lock:
            self._values.pop(key, None)


navigation_store = NavigationStore()
