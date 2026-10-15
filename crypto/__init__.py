"""Cryptocurrency price plugin (first-party module plugin).

Backfills daily close prices for the configured symbols from a public, **keyless**
source (Yahoo Finance) — from the earliest available record (or a configured start
date) up to today — into InfluxDB as a time series (measurement ``crypto_price``, tag
``symbol``, field ``close``, one point per day at that day's timestamp). It is
idempotent + incremental: each run resumes from the latest timestamp already
stored (the platform's ``timeseries.latest``), so the first run does the full
history and subsequent (registry-hourly) runs only fetch the new day(s). Financial
data, so no gaps and no double-counting.

``get_price`` is an on-demand action + Ollama tool returning the latest close.

Uses only deps already in the plugin-registry image (``httpx``) — Yahoo over its
public chart API. Points are written through the time-series handle the platform
binds at load (``bind_timeseries``): line protocol in, scoped to this plugin's
``TIMESERIES`` measurements. The plugin holds no InfluxDB address or token; with no
handle bound the sink is simply skipped.

Config (env on plugin-registry; all optional — keyless defaults):
  CRYPTO_SYMBOLS     Yahoo symbols, comma-sep (default "BTC-USD,ETH-USD"; use e.g.
                     "BTC-EUR" for another quote currency).
  CRYPTO_START_DATE  ISO date (YYYY-MM-DD) to backfill from when InfluxDB is empty;
                     default earliest Yahoo provides.
  CRYPTO_SINK_INFLUXDB "1"/"0" — write to InfluxDB (default "1").
  CRYPTO_HTTP_TIMEOUT  per-request timeout seconds (default 20).
"""

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from minder_plugin_sdk import PluginMetadata, line_protocol

__all__ = ["CryptoPlugin"]

logger = logging.getLogger("minder.plugin.crypto")

_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
# earliest we'll ever ask Yahoo for (it returns from the symbol's real start anyway).
_EARLIEST = date(2010, 1, 1)
# Friendly coin-id → Yahoo symbol aliases for the get_price tool.
_ALIASES = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "btc": "BTC-USD",
    "eth": "ETH-USD",
}
_MEASUREMENT = "crypto_price"
# The platform caps one timeseries.write batch (5,000 lines); a full backfill is
# written in ascending chunks below that, so a failed chunk leaves a clean
# resume point.
_WRITE_CHUNK = 2000

# Symbols come from config (CRYPTO_SYMBOLS, API-settable) and are used as a tag in
# line protocol and a resume lookup — restrict to a safe charset so a config
# value can't break out into injection (or corrupt line protocol with a space/comma).
# \Z (not $) -- $ matches just before a trailing newline too, so "BTC-USD\n" would
# pass this guard and then corrupt the line-protocol payload with an embedded newline.
_SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9._-]+\Z")


class CryptoPlugin:
    """Backfill + daily-incremental crypto close prices (Yahoo, keyless) into InfluxDB."""

    # Where this plugin connects (contract-v2 EGRESS declaration).
    EGRESS = ["query1.finance.yahoo.com"]
    # The time-series measurement this plugin writes (its platform scope).
    TIMESERIES = {"measurements": ["crypto_price"]}

    # The platform's time-series handle (bind_timeseries); None until bound.
    _timeseries: Any = None

    ACTIONS = frozenset({"refresh", "get_price"})
    # Read-only subset of ACTIONS, reachable unauthenticated via GET (#254) —
    # "refresh" stays POST + JWT-gated since it writes to InfluxDB.
    READ_ONLY_ACTIONS = frozenset({"get_price"})

    # Central config (#34) — GET/PUT /v1/plugins/crypto/config, applied live (no restart).
    _DEFAULT_SYMBOLS = "BTC-USD,ETH-USD"
    CONFIG_SCHEMA = [
        {
            "key": "CRYPTO_SYMBOLS",
            "type": "string",
            "default": _DEFAULT_SYMBOLS,
            "description": "Yahoo symbols to backfill, comma-separated (e.g. BTC-USD,ETH-USD).",
        },
        {
            "key": "CRYPTO_START_DATE",
            "type": "string",
            "default": "",
            "description": "ISO date to backfill from when empty; blank = earliest available.",
        },
        {
            "key": "CRYPTO_SINK_INFLUXDB",
            "type": "bool",
            "default": True,
            "description": "Write the price series to InfluxDB.",
        },
    ]

    AI_TOOLS = [
        {
            "name": "get_crypto_price",
            "description": (
                "Get the latest daily close price of a cryptocurrency (e.g. bitcoin, "
                "ethereum, or a Yahoo symbol like BTC-USD)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "coin": {
                        "type": "string",
                        "description": "Coin name/symbol, e.g. 'bitcoin', 'ETH-USD'.",
                    },
                },
                "required": ["coin"],
            },
            "action": "get_price",
            "method": "GET",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.http_timeout = float(
            os.environ.get("CRYPTO_HTTP_TIMEOUT", "20")
        )  # env-only
        self.status = "registered"
        self._last: Dict = {}
        # Bootstrap schema-backed config (defaults+env) via the single config→state
        # path; registry re-applies with persisted (API) overrides after load.
        self.apply_config(
            {
                "CRYPTO_SYMBOLS": os.environ.get(
                    "CRYPTO_SYMBOLS", self._DEFAULT_SYMBOLS
                ),
                "CRYPTO_START_DATE": os.environ.get("CRYPTO_START_DATE", ""),
                "CRYPTO_SINK_INFLUXDB": os.environ.get("CRYPTO_SINK_INFLUXDB", "1"),
            }
        )

    def apply_config(self, cfg: Dict) -> None:
        """Map centrally-managed config → runtime state (no restart). See CONFIG_SCHEMA."""
        if "CRYPTO_SYMBOLS" in cfg:
            self.symbols = [
                s.strip().upper()
                for s in str(cfg["CRYPTO_SYMBOLS"] or "").split(",")
                if s.strip()
            ]
        if "CRYPTO_START_DATE" in cfg:
            self.start_date = str(cfg["CRYPTO_START_DATE"] or "").strip()
        if "CRYPTO_SINK_INFLUXDB" in cfg:
            v = cfg["CRYPTO_SINK_INFLUXDB"]
            self.sink_influxdb = (
                v
                if isinstance(v, bool)
                else str(v).lower() in ("1", "true", "yes", "on")
            )

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="crypto",
            version="2.0.0",
            description="Backfills daily crypto close prices (Yahoo, keyless) into InfluxDB.",
            author="Minder <core@minder.local>",
            capabilities=["collect", "analyze", "prices", "backfill"],
            data_sources=["yahoo-finance"],
            databases=["influxdb"],
        )

    async def initialize(self) -> None:
        self.status = "ready"

    async def health_check(self) -> Dict:
        # MUST return {"healthy": <bool>} — the monitoring loop reads health["healthy"].
        return {
            "healthy": True,
            "symbols": self.symbols,
            "influxdb_sink": self.sink_influxdb,
        }

    async def shutdown(self) -> None:
        self.status = "shutdown"

    # ── Yahoo Finance history ──────────────────────────────────────────────────
    async def _fetch_history(
        self, symbol: str, start: date, end: date
    ) -> List[Tuple[int, float]]:
        """Return [(unix_seconds, close)] daily points for symbol in [start, end].

        Never raises — returns [] on any API/network error.
        """
        if not _SAFE_SYMBOL.match(symbol):
            # get_price() (an unauthenticated read action) builds `symbol` from
            # caller-supplied `coin` and passes it straight here -- unlike the
            # InfluxDB-side helpers above, this used to concatenate it directly
            # into the outbound Yahoo URL with no character-set check at all,
            # letting a caller inject query-string characters (?, &, #) into
            # the request. Host is hardcoded so this isn't SSRF to internal
            # infra, but it's the same unvalidated-input-into-URL shape the
            # news plugin's _is_safe_feed_url already guards against.
            logger.warning(f"⚠️ Refusing Yahoo fetch for unsafe symbol: {symbol!r}")
            return []
        p1 = int(
            datetime(
                start.year, start.month, start.day, tzinfo=timezone.utc
            ).timestamp()
        )
        p2 = (
            int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp())
            + 86400
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.http_timeout, headers={"User-Agent": "Mozilla/5.0"}
            ) as client:
                resp = await client.get(
                    _YAHOO_CHART + symbol,
                    params={"period1": p1, "period2": p2, "interval": "1d"},
                )
                resp.raise_for_status()
                res = ((resp.json() or {}).get("chart") or {}).get("result") or []
        except Exception as e:
            logger.warning(
                f"⚠️ Yahoo fetch failed for {symbol}: {type(e).__name__}: {e}"
            )
            return []
        if not res or not isinstance(res[0], dict):
            return []
        ts = res[0].get("timestamp") or []
        quote = (res[0].get("indicators") or {}).get("quote") or [{}]
        closes = quote[0].get("close") or []
        out: List[Tuple[int, float]] = []
        for t, c in zip(ts, closes):
            if isinstance(c, (int, float)):
                # normalise to that day's 00:00 UTC so re-runs overwrite, not duplicate.
                day = datetime.fromtimestamp(t, tz=timezone.utc).date()
                out.append(
                    (
                        int(
                            datetime(
                                day.year, day.month, day.day, tzinfo=timezone.utc
                            ).timestamp()
                        ),
                        float(c),
                    )
                )
        return out

    # ── time series (write history + query resume point) ───────────────────────
    def bind_timeseries(self, handle: Any) -> None:
        """Called by the host at load with this plugin's time-series handle:
        ``write(lines, precision)`` and ``latest(measurement, tag_key,
        tag_value)``, scoped to ``TIMESERIES``."""
        self._timeseries = handle

    def _sink(self) -> Any:
        return self._timeseries if self.sink_influxdb else None

    async def _latest_influx_date(self, symbol: str) -> Optional[date]:
        """Latest day already stored for symbol, or None (empty, sink off, or the
        store unavailable: the caller then backfills from the start)."""
        sink = self._sink()
        if sink is None or not _SAFE_SYMBOL.match(symbol):
            return None
        try:
            t = await sink.latest(_MEASUREMENT, "symbol", symbol)
        except Exception as e:
            logger.warning(f"⚠️ resume query failed for {symbol}: {type(e).__name__}")
            return None
        if not t:
            return None
        try:
            return datetime.fromisoformat(str(t).replace("Z", "")).date()
        except ValueError:
            return None

    async def _write_history(self, symbol: str, points: List[Tuple[int, float]]) -> int:
        """Write [(ts, close)]; return the count written (0 if sink off/empty)."""
        sink = self._sink()
        if sink is None or not points:
            return 0
        if not _SAFE_SYMBOL.match(symbol):
            logger.warning(f"⚠️ Skipping write for unsafe symbol: {symbol!r}")
            return 0
        # line_protocol escapes the tag and drops a non-finite close.
        lines = [
            ln
            for ts, close in points
            if (
                ln := line_protocol(
                    _MEASUREMENT, {"symbol": symbol}, {"close": close}, ts
                )
            )
        ]
        written = 0
        for i in range(0, len(lines), _WRITE_CHUNK):
            try:
                written += int(await sink.write(lines[i : i + _WRITE_CHUNK], "s"))
            except Exception as e:
                logger.warning(f"⚠️ write failed for {symbol}: {type(e).__name__}")
                break
        return written

    # ── registry-driven reads ────────────────────────────────────────────────
    async def collect_data(self) -> Dict:
        """Backfill/append each symbol's daily closes from the resume point to today."""
        today = datetime.now(timezone.utc).date()
        result: Dict[str, Dict] = {}
        for symbol in self.symbols:
            latest = await self._latest_influx_date(symbol)
            if latest is not None:
                start = latest + timedelta(days=1)  # incremental: only new days
            elif self.start_date:
                try:
                    start = date.fromisoformat(self.start_date)
                except ValueError:
                    start = _EARLIEST
            else:
                start = _EARLIEST  # full history (Yahoo trims to the real start)
            if start > today:
                result[symbol] = {"written": 0, "up_to_date": True}
                continue
            points = await self._fetch_history(symbol, start, today)
            wrote = await self._write_history(symbol, points)
            result[symbol] = {
                "written": wrote,
                "from": start.isoformat(),
                "latest_close": points[-1][1] if points else None,
            }
        self._last = {
            "symbols": result,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        total = sum(r.get("written", 0) for r in result.values())
        logger.info(
            f"💰 crypto collect: {total} point(s) across {len(result)} symbol(s)"
        )
        return self._last

    async def analyze(self) -> Dict:
        """Return the most recent collection summary."""
        if not self._last:
            return {"message": "no data collected yet", "symbols": self.symbols}
        return self._last

    # ── actions (POST /v1/plugins/crypto/actions/<method>, JWT-gated) ──────────
    async def refresh(self) -> Dict:
        """Force an immediate backfill/append (same as the hourly loop)."""
        return await self.collect_data()

    async def get_price(self, coin: str) -> Dict:
        """Return the latest daily close for a coin/symbol (backs get_crypto_price)."""
        if not coin:
            return {"error": "coin is required"}
        key = coin.strip().lower()
        symbol = _ALIASES.get(key, coin.strip().upper())
        if "-" not in symbol:
            symbol = f"{symbol}-USD"
        today = datetime.now(timezone.utc).date()
        points = await self._fetch_history(symbol, today - timedelta(days=7), today)
        if not points:
            return {"symbol": symbol, "error": "price unavailable"}
        return {"symbol": symbol, "close": points[-1][1]}
