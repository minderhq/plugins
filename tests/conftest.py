"""Shared test helpers.

``FakeTimeseries`` stands in for the time-series handle the platform binds to a
plugin at load (``plugin.bind_timeseries(handle)``): ``write(lines, precision)``
returns the number of points written and ``latest(measurement, tag_key,
tag_value)`` the newest timestamp (ISO 8601) or ``None``. Like the platform, it
refuses a line whose measurement is outside the plugin's declared
``TIMESERIES`` and a batch over the platform's line cap.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest

#: The platform's per-batch line cap.
MAX_BATCH_LINES = 5000


class TimeseriesRefused(Exception):
    """What the fake raises for an out-of-scope or oversized write."""


class FakeTimeseries:
    def __init__(
        self,
        measurements: Optional[List[str]] = None,
        latest: Optional[Dict[Tuple[str, str, str], Optional[str]]] = None,
        fail_write: Optional[Exception] = None,
        fail_latest: Optional[Exception] = None,
    ) -> None:
        self.measurements = measurements
        self.latest_values = dict(latest or {})
        self.fail_write = fail_write
        self.fail_latest = fail_latest
        self.batches: List[Tuple[List[str], str]] = []
        self.latest_calls: List[Tuple[str, str, str]] = []

    @property
    def lines(self) -> List[str]:
        return [line for batch, _ in self.batches for line in batch]

    async def write(self, lines: Any, precision: str = "s") -> int:
        if self.fail_write is not None:
            raise self.fail_write
        batch = [lines] if isinstance(lines, str) else list(lines)
        if len(batch) > MAX_BATCH_LINES:
            raise TimeseriesRefused(f"batch longer than {MAX_BATCH_LINES} lines")
        for line in batch:
            measurement = line.split(",", 1)[0].split(" ", 1)[0]
            if self.measurements is not None and measurement not in self.measurements:
                raise TimeseriesRefused(f"measurement {measurement!r} not in scope")
        self.batches.append((batch, precision))
        return len(batch)

    async def latest(
        self, measurement: str, tag_key: str, tag_value: str
    ) -> Optional[str]:
        self.latest_calls.append((measurement, tag_key, tag_value))
        if self.fail_latest is not None:
            raise self.fail_latest
        return self.latest_values.get((measurement, tag_key, tag_value))


@pytest.fixture
def bind_timeseries():
    """``bind_timeseries(plugin, **kwargs)``: bind a FakeTimeseries scoped to the
    plugin's declared TIMESERIES measurements and return it."""

    def _bind(plugin: Any, **kwargs: Any) -> FakeTimeseries:
        declared = getattr(plugin, "TIMESERIES", None) or {}
        kwargs.setdefault("measurements", list(declared.get("measurements", [])))
        handle = FakeTimeseries(**kwargs)
        plugin.bind_timeseries(handle)
        return handle

    return _bind
