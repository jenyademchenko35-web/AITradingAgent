"""Small registry-based router for Telegram UI v2 screens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


class UnknownScreen(LookupError):
    """Raised when a callback targets an unregistered screen."""


@dataclass(frozen=True)
class ScreenRequest:
    name: str
    arguments: tuple[str, ...] = ()
    user_id: object = None


ScreenHandler = Callable[[ScreenRequest], tuple[str, Any]]


class ScreenRouter:
    """Dispatch immutable screen requests through an explicit route table."""

    def __init__(self, routes: Mapping[str, ScreenHandler]) -> None:
        self._routes = dict(routes)

    @property
    def screens(self) -> frozenset[str]:
        return frozenset(self._routes)

    def render(self, request: ScreenRequest) -> tuple[str, Any]:
        handler = self._routes.get(request.name)
        if handler is None:
            raise UnknownScreen(request.name)
        return handler(request)
