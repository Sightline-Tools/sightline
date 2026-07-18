from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Callable


@dataclass(frozen=True)
class StructuredCombatEvent:
    event_type: str
    amount: int | None
    source: str | None
    target: str | None
    timestamp: datetime
    ability_name: str | None = None


CombatEventSubscriber = Callable[[StructuredCombatEvent], None]


class CombatEventBus:
    def __init__(self) -> None:
        self._subscribers: list[CombatEventSubscriber] = []
        self._lock = Lock()

    def subscribe(self, subscriber: CombatEventSubscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def _unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return _unsubscribe

    def publish(self, event: StructuredCombatEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber(event)


combat_event_bus = CombatEventBus()
