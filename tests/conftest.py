"""Shared test helpers.

``FakeTimeseries`` stands in for the time-series handle the platform binds to a
plugin at load (``plugin.bind_timeseries(handle)``): ``write(lines, precision)``
returns the number of points written and ``latest(measurement, tag_key,
tag_value)`` the newest timestamp (ISO 8601) or ``None``. With
``fields=[...]``, ``latest`` returns ``{"time", "fields": {name: value}}`` of
the newest point instead. Like the platform, it refuses a line whose
measurement is outside the plugin's declared ``TIMESERIES`` and a batch over
the platform's line cap, and it keeps each ``owner``'s points apart (set
``handle.owner`` to the owner the platform would be acting for).
"""

import re
from datetime import datetime, timezone
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
        self.latest_fields_calls: List[Tuple[str, str, str, List[str]]] = []
        #: The owner the platform is acting for (it forces the owner tag).
        self.owner: Optional[str] = None
        #: (owner, measurement, tag_key, tag_value) -> (ts, fields) of the
        #: newest point written.
        self.points: Dict[Tuple[Any, str, str, str], Tuple[int, Dict[str, Any]]] = {}

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
        for line in batch:
            self._store(line)
        return len(batch)

    def _store(self, line: str) -> None:
        head, fields, ts = _split_line(line)
        measurement, *tags = head.split(",")
        for tag in tags:
            key, _, value = tag.partition("=")
            slot = (self.owner, measurement, key, value)
            if slot not in self.points or self.points[slot][0] <= ts:
                self.points[slot] = (ts, fields)

    async def latest(
        self,
        measurement: str,
        tag_key: str,
        tag_value: str,
        fields: Optional[List[str]] = None,
    ) -> Any:
        if fields is None:
            self.latest_calls.append((measurement, tag_key, tag_value))
        else:
            self.latest_fields_calls.append(
                (measurement, tag_key, tag_value, list(fields))
            )
        if self.fail_latest is not None:
            raise self.fail_latest
        if fields is None:
            return self.latest_values.get((measurement, tag_key, tag_value))
        found = self.points.get((self.owner, measurement, tag_key, tag_value))
        if found is None:
            return None
        ts, values = found
        return {
            "time": datetime.fromtimestamp(ts, tz=timezone.utc)
            .replace(tzinfo=None)
            .isoformat(),
            "fields": {name: values.get(name) for name in fields},
        }


_LINE = re.compile(r"^(\S+) (.*?)(?: (\d+))?$")
_FIELD = re.compile(r'([^=,]+)=("(?:[^"\\]|\\.)*"|[^,]*)')


def _split_line(line: str) -> Tuple[str, Dict[str, Any], int]:
    """The test-only parse of one line this catalog writes: unescaped tags,
    then fields, then an optional timestamp."""
    match = _LINE.match(line)
    assert match, line
    head, body, ts = match.groups()
    fields: Dict[str, Any] = {}
    for key, raw in _FIELD.findall(body):
        if raw.startswith('"'):
            fields[key] = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif raw.endswith("i") and raw[:-1].lstrip("-").isdigit():
            fields[key] = int(raw[:-1])
        elif raw in ("true", "false"):
            fields[key] = raw == "true"
        else:
            fields[key] = float(raw)
    return head, fields, int(ts) if ts else 0


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
