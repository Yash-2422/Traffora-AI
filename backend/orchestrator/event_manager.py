"""Event intake for the orchestrator.

Agents publish *facts* (a congestion alert, an emergency detection, a parking
change). The event manager validates them, drops duplicates inside a time
window, keeps a bounded priority queue and exposes deterministic ordering:
emergency first, then traffic, then parking; ties are broken by simulation time
and insertion order.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Iterable, List, Optional

from .exceptions import DuplicateEventError, EventValidationError
from .schemas import AgentName, Event, EventPriority, EventStatus, EventType

logger = logging.getLogger(__name__)

# Event type -> mandatory payload keys.
_REQUIRED_PAYLOAD: Dict[EventType, tuple] = {
    EventType.CONGESTION_DETECTED: ("junction_id", "edge_id"),  # at least one
    EventType.CONGESTION_PREDICTED: ("junction_id",),
    EventType.EMERGENCY_DETECTED: ("vehicle_id",),
    EventType.EMERGENCY_ROUTE_UPDATE: ("vehicle_id",),
    EventType.SIGNAL_PRIORITY_REQUEST: ("vehicle_id", "tls_id"),
    EventType.PARKING_OCCUPANCY_CHANGED: ("overall_occupancy_pct",),
    EventType.PARKING_DEMAND_ALERT: ("zone_id",),
}

# Event type -> canonical priority.
_PRIORITY_BY_TYPE: Dict[EventType, EventPriority] = {
    EventType.EMERGENCY_DETECTED: EventPriority.EMERGENCY,
    EventType.EMERGENCY_ROUTE_UPDATE: EventPriority.EMERGENCY,
    EventType.SIGNAL_PRIORITY_REQUEST: EventPriority.EMERGENCY,
    EventType.CONGESTION_DETECTED: EventPriority.TRAFFIC,
    EventType.CONGESTION_PREDICTED: EventPriority.TRAFFIC,
    EventType.PARKING_OCCUPANCY_CHANGED: EventPriority.PARKING,
    EventType.PARKING_DEMAND_ALERT: EventPriority.PARKING,
}


class EventManager:
    """Validated, deduplicated, priority-ordered event queue."""

    def __init__(self, dedupe_window_s: float = 30.0, max_pending: int = 1000,
                 history_size: int = 1000) -> None:
        if dedupe_window_s < 0:
            raise EventValidationError("dedupe_window_s must be >= 0")
        if max_pending < 1:
            raise EventValidationError("max_pending must be >= 1")
        self._dedupe_window_s = float(dedupe_window_s)
        self._max_pending = int(max_pending)
        self._queue: List[Event] = []
        self._history: Deque[Event] = deque(maxlen=history_size)
        self._last_seen: Dict[str, float] = {}
        self._sequence = 0
        self._duplicates = 0
        self._rejected = 0
        self._processed = 0

    # ------------------------------------------------------------------ #
    # Creation and validation
    # ------------------------------------------------------------------ #
    @staticmethod
    def priority_for(event_type: EventType) -> EventPriority:
        return _PRIORITY_BY_TYPE[event_type]

    def create_event(self, event_type: EventType, source_agent: AgentName,
                     timestamp_s: float, payload: Optional[Dict] = None,
                     event_id: Optional[str] = None,
                     dedupe_key: Optional[str] = None) -> Event:
        """Build (but do not enqueue) a validated event.

        Validation errors are always raised as :class:`EventValidationError` so
        callers only ever have to catch one exception type, even though the
        schema itself also rejects bad values.
        """
        try:
            timestamp_s = float(timestamp_s)
        except (TypeError, ValueError) as exc:
            raise EventValidationError(
                f"timestamp_s must be a number, got {timestamp_s!r}") from exc
        if not math.isfinite(timestamp_s) or timestamp_s < 0:
            raise EventValidationError(
                f"event timestamp_s must be a finite value >= 0, got {timestamp_s!r}")
        event = Event(
            event_id=event_id or f"evt-{uuid.uuid4().hex[:12]}",
            event_type=event_type,
            source_agent=source_agent,
            timestamp_s=float(timestamp_s),
            priority=self.priority_for(event_type),
            payload=payload or {},
            dedupe_key=dedupe_key,
        )
        self._validate(event)
        return event

    def _validate(self, event: Event) -> None:
        if event.timestamp_s < 0:
            raise EventValidationError("event timestamp_s must be >= 0")
        if not event.event_id:
            raise EventValidationError("event_id must be non-empty")
        expected = _PRIORITY_BY_TYPE.get(event.event_type)
        if expected is None:
            raise EventValidationError(f"Unsupported event type: {event.event_type}")
        if event.priority != expected:
            raise EventValidationError(
                f"{event.event_type.value} must have priority {expected.label}, "
                f"got {event.priority.label}")
        required = _REQUIRED_PAYLOAD.get(event.event_type, ())
        if event.event_type is EventType.CONGESTION_DETECTED:
            if not (event.payload.get("junction_id") or event.payload.get("edge_id")):
                raise EventValidationError(
                    "CONGESTION_DETECTED needs 'junction_id' or 'edge_id'")
        else:
            missing = [key for key in required if event.payload.get(key) in (None, "")]
            if missing:
                raise EventValidationError(
                    f"{event.event_type.value} is missing payload field(s): {', '.join(missing)}")

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    def publish(self, event: Event) -> Event:
        """Validate, deduplicate and enqueue. Raises on invalid/duplicate."""
        self._validate(event)
        key = event.stable_key()
        last = self._last_seen.get(key)
        if last is not None and abs(event.timestamp_s - last) <= self._dedupe_window_s:
            self._duplicates += 1
            raise DuplicateEventError(
                f"Duplicate event '{key}' within {self._dedupe_window_s}s "
                f"(last at {last}, now {event.timestamp_s})")
        if len(self._queue) >= self._max_pending:
            self._rejected += 1
            raise EventValidationError(
                f"Event queue is full ({self._max_pending}); '{key}' rejected")
        self._last_seen[key] = event.timestamp_s
        self._sequence += 1
        event.status = EventStatus.PENDING
        self._queue.append(event)
        return event

    def try_publish(self, event: Event) -> bool:
        """Non-raising variant used by the orchestrator loop."""
        try:
            self.publish(event)
            return True
        except DuplicateEventError as exc:
            logger.debug("dropped duplicate event: %s", exc)
            return False
        except EventValidationError as exc:
            logger.warning("dropped invalid event: %s", exc)
            return False

    def publish_many(self, events: Iterable[Event]) -> int:
        accepted = 0
        for event in events:
            if self.try_publish(event):
                accepted += 1
        return accepted

    # ------------------------------------------------------------------ #
    # Consumption
    # ------------------------------------------------------------------ #
    def drain(self, limit: Optional[int] = None) -> List[Event]:
        """Return pending events in processing order and mark them PROCESSING."""
        ordered = sorted(self._queue, key=lambda e: (int(e.priority), e.timestamp_s,
                                                     e.created_at))
        if limit is not None:
            ordered = ordered[:max(0, limit)]
        selected_ids = {e.event_id for e in ordered}
        self._queue = [e for e in self._queue if e.event_id not in selected_ids]
        for event in ordered:
            event.status = EventStatus.PROCESSING
        return ordered

    def mark_processed(self, event: Event, status: EventStatus = EventStatus.PROCESSED,
                       error: Optional[str] = None) -> None:
        if status not in (EventStatus.PROCESSED, EventStatus.REJECTED, EventStatus.FAILED):
            raise EventValidationError(f"Cannot finish an event with status {status}")
        event.status = status
        event.error = error
        event.processed_at = datetime.now(timezone.utc)
        self._history.append(event)
        if status is EventStatus.PROCESSED:
            self._processed += 1
        else:
            self._rejected += 1

    def pending(self) -> List[Event]:
        return sorted(self._queue, key=lambda e: (int(e.priority), e.timestamp_s,
                                                  e.created_at))

    def history(self, limit: Optional[int] = None) -> List[Event]:
        items = list(self._history)
        return items[-limit:] if limit else items

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    @property
    def pending_count(self) -> int:
        return len(self._queue)

    @property
    def processed_count(self) -> int:
        return self._processed

    @property
    def rejected_count(self) -> int:
        return self._rejected

    @property
    def duplicate_count(self) -> int:
        return self._duplicates

    def stats(self) -> Dict[str, int]:
        return {
            "pending": self.pending_count,
            "processed": self.processed_count,
            "rejected": self.rejected_count,
            "duplicates": self.duplicate_count,
        }

    def clear(self) -> None:
        self._queue.clear()
        self._history.clear()
        self._last_seen.clear()
        self._sequence = 0
        self._duplicates = 0
        self._rejected = 0
        self._processed = 0
