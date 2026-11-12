"""Routing Campfire events to handlers. Modules register for the topics they care about.

Handlers run inline for now (they must be quick and idempotent). A task queue replaces this when
posting/reconciliation handlers arrive.
"""

import logging
from collections import defaultdict
from collections.abc import Callable

from django.utils import timezone

from campfire_integration.models import CampfireEvent

log = logging.getLogger(__name__)

Handler = Callable[[CampfireEvent], None]
_handlers: dict[str, list[Handler]] = defaultdict(list)


def on(*topics: str) -> Callable[[Handler], Handler]:
    """Decorator: `@events.on("Bill.updated")`."""

    def register(fn: Handler) -> Handler:
        for topic in topics:
            _handlers[topic].append(fn)
        return fn

    return register


def dispatch(event: CampfireEvent) -> None:
    errors: list[str] = []
    for handler in _handlers.get(event.topic, []):
        try:
            handler(event)
        except Exception as e:  # one failing handler must not lose the event for the others
            log.exception("handler %s failed for event %s", handler.__name__, event.pk)
            errors.append(f"{handler.__name__}: {e}")
    event.processed_at = timezone.now()
    event.error = "\n".join(errors)
    event.save(update_fields=["processed_at", "error"])
