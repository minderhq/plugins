"""TEFAS fund-price plugin (first-party module plugin).

Backfills daily fund prices from TEFAS (Türkiye Elektronik Fon Alım Satım Platformu)
for the configured fund codes — from a configured start date (or a sane default) up to
today — into InfluxDB (measurement ``tefas_fund_price``, tag ``code``, field ``price``,
one point per publication day). Idempotent + incremental like the crypto plugin: each
run resumes from the latest timestamp already stored. Financial data → no gaps /
no double-counting.

Points are written through the time-series handle the platform binds at load
(``bind_timeseries``), scoped to this plugin's ``TIMESERIES`` measurements. The
plugin holds no InfluxDB address or token; with no handle bound the sink is skipped.

Source: the ``tefas-crawler`` library (in the registry image). It's synchronous and
TEFAS rate-limits/robot-checks automated access, so fetches run in a worker thread and
fail soft (a blocked/empty fetch logs a warning and returns nothing rather than
crashing the collection loop).

Config is managed centrally over the API (CONFIG_SCHEMA — GET/PUT
/v1/plugins/tefas/config):
  TEFAS_FUNDS        comma-sep TEFAS fund codes (e.g. "AFA,AAK,IPB"); empty = none.
  TEFAS_START_DATE   ISO date to backfill from when InfluxDB is empty (default 2015-01-01).
  TEFAS_SINK_INFLUXDB "1"/"0" — write the series to InfluxDB (default "1").

NOTE: TEFAS actively anti-bots non-Turkish/datacenter egress, so live data-fetch is
only verifiable from an unblocked network (the Pi / a TR ISP). The plugin loads,
configures, and fails soft everywhere; the data lands where TEFAS is reachable.
"""

import asyncio
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from minder_plugin_sdk import PluginMetadata, line_protocol

__all__ = ["TefasPlugin"]

logger = logging.getLogger("minder.plugin.tefas")

_MEASUREMENT = "tefas_fund_price"
_DEFAULT_START = "2015-01-01"
# The platform caps one timeseries.write batch (5,000 lines); a long backfill is
# written in ascending chunks below that, so a failed chunk leaves a clean
# resume point.
_WRITE_CHUNK = 2000

# Fund codes come from config (TEFAS_FUNDS, API-settable) and are used as a tag in
# line protocol and a resume lookup — restrict to a safe charset so a config
# value can't break out into injection (or corrupt line protocol with a space/comma).
# \Z (not $) -- $ matches just before a trailing newline too, so "CODE\n" would pass
# this guard and then corrupt the line-protocol payload with an embedded newline.
_SAFE_CODE = re.compile(r"^[A-Za-z0-9._-]+\Z")

# The `tefas` import below is the pip **tefas-crawler** package. This plugin's
# directory is `tefas_funds` (deliberately NOT `tefas`) so it never shadows that
# package — no sys.path juggling needed.
try:
    from tefas import Crawler  # type: ignore[import-untyped]

    TEFAS_AVAILABLE = True
except ImportError:  # pragma: no cover
    Crawler = None  # type: ignore[assignment,misc]
    TEFAS_AVAILABLE = False
    logging.warning("tefas-crawler not installed; TEFAS plugin will no-op")


class TefasPlugin:
    """Backfill + daily-incremental TEFAS fund prices into InfluxDB (tefas-crawler)."""

    # Where this plugin connects (contract-v2 EGRESS declaration): via the tefas-crawler library.
    EGRESS = ["www.tefas.gov.tr"]
    # The time-series measurement this plugin writes (its platform scope).
    TIMESERIES = {"measurements": ["tefas_fund_price"]}

    # The platform's time-series handle (bind_timeseries); None until bound.
    _timeseries: Any = None

    ACTIONS = frozenset({"refresh", "get_fund_price"})
    # Read-only subset of ACTIONS, reachable unauthenticated via GET (#254) —
    # "refresh" stays POST + JWT-gated since it writes to InfluxDB.
    READ_ONLY_ACTIONS = frozenset({"get_fund_price"})

    CONFIG_SCHEMA = [
        {
            "key": "TEFAS_FUNDS",
            "type": "string",
            "default": "",
            "description": "TEFAS fund codes to track, comma-separated (e.g. AFA,AAK,IPB).",
        },
        {
            "key": "TEFAS_START_DATE",
            "type": "string",
            "default": _DEFAULT_START,
            "description": "ISO date to backfill from when InfluxDB is empty.",
        },
        {
            "key": "TEFAS_SINK_INFLUXDB",
            "type": "bool",
            "default": True,
            "description": "Write the fund-price series to InfluxDB.",
        },
    ]

    AI_TOOLS = [
        {
            "name": "get_fund_price",
            "description": (
                "Get the latest daily price of a Turkish TEFAS investment fund by its "
                "fund code (e.g. AFA, AAK)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "TEFAS fund code, e.g. 'AFA'.",
                    },
                },
                "required": ["code"],
            },
            "action": "get_fund_price",
            "method": "GET",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.http_timeout = float(
            os.environ.get("TEFAS_HTTP_TIMEOUT", "30")
        )  # env-only
        self.status = "registered"
        self._last: Dict = {}
        self.apply_config(
            {
                "TEFAS_FUNDS": os.environ.get("TEFAS_FUNDS", ""),
                "TEFAS_START_DATE": os.environ.get("TEFAS_START_DATE", _DEFAULT_START),
                "TEFAS_SINK_INFLUXDB": os.environ.get("TEFAS_SINK_INFLUXDB", "1"),
            }
        )

    def apply_config(self, cfg: Dict) -> None:
        """Map centrally-managed config → runtime state (no restart). See CONFIG_SCHEMA."""
        if "TEFAS_FUNDS" in cfg:
            self.funds = [
                c.strip().upper()
                for c in str(cfg["TEFAS_FUNDS"] or "").split(",")
                if c.strip()
            ]
        if "TEFAS_START_DATE" in cfg:
            self.start_date = str(cfg["TEFAS_START_DATE"] or _DEFAULT_START).strip()
        if "TEFAS_SINK_INFLUXDB" in cfg:
            v = cfg["TEFAS_SINK_INFLUXDB"]
            self.sink_influxdb = (
                v
                if isinstance(v, bool)
                else str(v).lower() in ("1", "true", "yes", "on")
            )

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="tefas",
            version="1.0.0",
            description="Backfills daily TEFAS fund prices into InfluxDB (tefas-crawler).",
            author="Minder <core@minder.local>",
            capabilities=["collect", "analyze", "funds", "backfill"],
            data_sources=["tefas"],
            databases=["influxdb"],
        )

    async def initialize(self) -> None:
        self.status = "ready"

    async def health_check(self) -> Dict:
        # MUST return {"healthy": <bool>} — the monitoring loop reads health["healthy"].
        return {
            "healthy": True,
            "tefas_available": TEFAS_AVAILABLE,
            "funds": self.funds,
            # Points are written only with the toggle on AND a time-series
            # handle bound by the host.
            "influxdb_sink": self.sink_influxdb and self._timeseries is not None,
            "timeseries_bound": self._timeseries is not None,
        }

    async def shutdown(self) -> None:
        self.status = "shutdown"

    # ── TEFAS fetch (tefas-crawler is sync + anti-botted → to_thread + fail soft) ──
    def _fetch_sync(self, code: str, start: date, end: date) -> List[Tuple[int, float]]:
        """Blocking tefas-crawler fetch → [(unix_seconds, price)]. [] on error/empty."""
        try:
            df = Crawler().fetch(
                start=start.isoformat(),
                end=end.isoformat(),
                name=code,
                columns=["date", "code", "price"],
            )
        except Exception as e:
            logger.warning(f"⚠️ TEFAS fetch failed for {code}: {type(e).__name__}: {e}")
            return []
        out: List[Tuple[int, float]] = []
        try:
            for _, row in df.iterrows():
                d = row["date"]
                day = (
                    d.date() if hasattr(d, "date") else date.fromisoformat(str(d)[:10])
                )
                price = row["price"]
                # skip None AND non-finite (pandas NaN for a gap day is a float, not
                # None — it would become `price=nan` and 400 the whole batch, stalling
                # the incremental resume).
                if price is not None and math.isfinite(float(price)):
                    ts = int(
                        datetime(
                            day.year, day.month, day.day, tzinfo=timezone.utc
                        ).timestamp()
                    )
                    out.append((ts, float(price)))
        except Exception as e:
            logger.warning(f"⚠️ TEFAS parse failed for {code}: {type(e).__name__}")
            return []
        return out

    async def _fetch_history(
        self, code: str, start: date, end: date
    ) -> List[Tuple[int, float]]:
        if not TEFAS_AVAILABLE:
            return []
        return await asyncio.to_thread(self._fetch_sync, code, start, end)

    # ── time series (mirror crypto: write history + query resume point) ────────
    def bind_timeseries(self, handle: Any) -> None:
        """Called by the host at load with this plugin's time-series handle:
        ``write(lines, precision)`` and ``latest(measurement, tag_key,
        tag_value)``, scoped to ``TIMESERIES``."""
        self._timeseries = handle

    def _sink(self) -> Any:
        return self._timeseries if self.sink_influxdb else None

    async def _latest_influx_date(self, code: str) -> Optional[date]:
        """Latest day already stored for code, or None (empty, sink off, or the
        store unavailable: the caller then backfills from the start date)."""
        sink = self._sink()
        if sink is None or not _SAFE_CODE.match(code):
            return None
        try:
            t = await sink.latest(_MEASUREMENT, "code", code)
        except Exception as e:
            logger.warning(f"⚠️ resume query failed for {code}: {type(e).__name__}")
            return None
        if not t:
            return None
        try:
            return datetime.fromisoformat(str(t).replace("Z", "")).date()
        except ValueError:
            return None

    async def _write_history(self, code: str, points: List[Tuple[int, float]]) -> int:
        """Write [(ts, price)]; return the count written (0 if sink off/empty)."""
        sink = self._sink()
        if sink is None or not points:
            return 0
        if not _SAFE_CODE.match(code):
            logger.warning(f"⚠️ Skipping write for unsafe code: {code!r}")
            return 0
        # line_protocol escapes the tag and drops a non-finite price.
        lines = [
            ln
            for ts, price in points
            if (ln := line_protocol(_MEASUREMENT, {"code": code}, {"price": price}, ts))
        ]
        written = 0
        for i in range(0, len(lines), _WRITE_CHUNK):
            try:
                written += int(await sink.write(lines[i : i + _WRITE_CHUNK], "s"))
            except Exception as e:
                logger.warning(f"⚠️ write failed for {code}: {type(e).__name__}")
                break
        return written

    # ── registry-driven reads ────────────────────────────────────────────────
    async def collect_data(self) -> Dict:
        """Backfill/append each fund's daily prices from the resume point to today."""
        today = datetime.now(timezone.utc).date()
        result: Dict[str, Dict] = {}
        for code in self.funds:
            latest = await self._latest_influx_date(code)
            if latest is not None:
                start = latest + timedelta(days=1)
            else:
                try:
                    start = date.fromisoformat(self.start_date)
                except ValueError:
                    start = date.fromisoformat(_DEFAULT_START)
            if start > today:
                result[code] = {"written": 0, "up_to_date": True}
                continue
            points = await self._fetch_history(code, start, today)
            wrote = await self._write_history(code, points)
            result[code] = {
                "written": wrote,
                "from": start.isoformat(),
                "latest_price": points[-1][1] if points else None,
            }
        self._last = {
            "funds": result,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        total = sum(r.get("written", 0) for r in result.values())
        logger.info(f"📊 tefas collect: {total} point(s) across {len(result)} fund(s)")
        return self._last

    async def analyze(self) -> Dict:
        if not self._last:
            return {"message": "no data collected yet", "funds": self.funds}
        return self._last

    # ── actions (POST /v1/plugins/tefas/actions/<method>, JWT-gated) ───────────
    async def refresh(self) -> Dict:
        """Force an immediate backfill/append (same as the hourly loop)."""
        return await self.collect_data()

    async def get_fund_price(self, code: str) -> Dict:
        """Return the latest daily price for a fund code (backs get_fund_price tool)."""
        if not code:
            return {"error": "code is required"}
        code = code.strip().upper()
        today = datetime.now(timezone.utc).date()
        points = await self._fetch_history(code, today - timedelta(days=10), today)
        if not points:
            return {"code": code, "error": "price unavailable"}
        return {"code": code, "price": points[-1][1]}
