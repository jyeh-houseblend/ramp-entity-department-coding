"""Tiny in-memory TTL cache for user / fund / reference-data lookups."""
from __future__ import annotations

import time
from typing import Callable, Generic, Hashable, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[Hashable, tuple[float, V]] = {}

    def get_or_load(self, key: Hashable, loader: Callable[[], V]) -> V:
        hit = self._data.get(key)
        now = self._clock()
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        value = loader()
        self._data[key] = (now, value)
        return value

    def clear(self) -> None:
        self._data.clear()
