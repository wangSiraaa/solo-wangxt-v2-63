"""Injectable clock.

All time-based domain logic (SLA timers, escalation, contract matching) goes
through ``Clock.now()``.  Tests pass a fixed clock; the default implementation
reads wall-clock UTC, so production code does not depend on the test seam.
"""
from datetime import datetime, timezone
from typing import Optional


class Clock:
    _override: Optional[datetime] = None

    @classmethod
    def now(cls) -> datetime:
        if cls._override is not None:
            return cls._override
        return datetime.now(timezone.utc)

    @classmethod
    def set(cls, value: datetime) -> None:
        """Pin the clock to an aware UTC datetime."""
        if value.tzinfo is None:
            raise ValueError("clock requires a timezone-aware datetime")
        cls._override = value.astimezone(timezone.utc)

    @classmethod
    def reset(cls) -> None:
        cls._override = None
