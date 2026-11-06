import logging
from collections.abc import Callable


class EventBus:
    def __init__(self):
        self._listeners: list[Callable[[dict], None]] = []

    def subscribe(self, listener: Callable[[dict], None]) -> None:
        self._listeners.append(listener)

    def emit(self, event: dict) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(dict(event))
            except Exception:
                logging.getLogger("better.events").exception("observer_failed")
