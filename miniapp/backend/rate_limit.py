"""Small process-local limiter for the owner-only read-only API."""

from __future__ import annotations

from collections import defaultdict, deque
import threading
import time

from fastapi import HTTPException, status


class InMemoryRateLimiter:
    def __init__(self, requests: int = 60, window_seconds: int = 60) -> None:
        self.requests = max(1, int(requests))
        self.window_seconds = max(1, int(window_seconds))
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def key(*, user_id: int | None, client_ip: str | None) -> str:
        return f"user:{user_id}" if user_id is not None else f"ip:{client_ip or 'unknown'}"

    def allow(self, *, user_id: int | None = None, client_ip: str | None = None,
              now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        key = self.key(user_id=user_id, client_ip=client_ip)
        cutoff = current - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                return False
            events.append(current)
            return True

    def require(self, *, user_id: int | None = None, client_ip: str | None = None) -> None:
        if not self.allow(user_id=user_id, client_ip=client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(self.window_seconds)},
            )
