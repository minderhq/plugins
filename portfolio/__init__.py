"""Personal portfolio/watchlist plugin (first-party module plugin, slice 6.4).

Minder's first genuinely PRIVATE-per-user data plugin. The other 4 shipped
data plugins (crypto, weather, news, tefas) all collect SHARED public
reference data — same value for every user, correctly written to InfluxDB
tagged ``owner_id='shared'``. This plugin is the opposite: each user records
which assets they personally hold/watch (``PORTFOLIO_HOLDINGS``, a per-user
config layer, #920), and each user's price history is written under their
OWN ``owner_id`` tag — the concrete answer to issue #992's finding that the
correlation engine's temporal + entity↔signal correlators had no genuinely
private time-series data to work with.

Reuses the same public, keyless Yahoo Finance chart API the crypto plugin
already uses (``query1.finance.yahoo.com/v8/finance/chart/<symbol>``) — it's
a general financial-data endpoint, not crypto-specific, so any Yahoo-tradable
symbol works here (stocks, ETFs, crypto pairs), unlike crypto's own
crypto-only framing.

``PRIVATE_PER_USER = True`` opts this plugin out of plugin-registry's
fingerprint-dedup collection scheduler (core/monitoring.py's
``collect_plugin_data``, #920 slice 3.3 — correct for shared reference data,
wrong here: two users with an identical watchlist still each need their own
collection run and their own private rows) and into the new per-owner path
(``_collect_private_plugin_data``), which threads ``_owner_id`` into
``apply_config``'s dict — the plugin never receives an owner_id as a real
argument (the plugin Protocol's ``collect_data()`` takes none), matching how
every other plugin already gets runtime config via ``apply_config``.

Points are written through the time-series handle the platform binds at load
(``bind_timeseries``), scoped to this plugin's ``TIMESERIES`` measurement. The
plugin holds no InfluxDB address or token, and it does not tag the owner itself:
the platform forces the ``owner_id`` tag to the owner it is collecting for, so a
point can never land under another user's (or the shared) owner.

Exposes a single owner-scoped read-back action (#1035): ``get_value`` (AI tool
``get_portfolio_value``) returns the latest recorded price for each symbol THIS
user tracks: the last point this instance wrote for that owner (the platform's
time-series read returns timestamps, not values, so the plugin keeps that last
point per owner in memory; it is refilled by the next scheduled collection after
a restart). It is a
PRIVATE_PER_USER action, so plugin-registry's action dispatch
(``routes/plugins.py``) injects the AUTHENTICATED caller's own ``owner_id`` from
the JWT — never caller-supplied — so a user can only ever read their own
holdings. This closes the pre-existing "an on-demand action can't resolve the
calling user's identity into the plugin method" gap that previously left this
plugin config-only. The scheduled per-owner collection (above) remains the write
path; ``get_value`` is the read path over the same private series.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from minder_plugin_sdk import PluginMetadata, line_protocol

__all__ = ["PortfolioPlugin"]

logger = logging.getLogger("minder.plugin.portfolio")

_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
_MEASUREMENT = "portfolio_holding"

# Same charset guard as crypto/tefas: holdings symbols come from per-user
# config (API-settable) and are used as a line-protocol tag -- \Z (not $) so a
# trailing "\n" can't sneak through and corrupt the payload with an embedded
# newline.
_SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9._-]+\Z")


def _owner_tag(owner: str) -> str:
    """Scrub an owner_id (a JWT sub — may be ``auth0|123``, an email, ``a:b``…) to
    the safe charset the platform uses for the ``owner_id`` tag it forces on
    every point. Used as the key of the per-owner record ``get_value`` reads, so
    the write and read paths agree (``auth0|123`` -> ``auth0_123`` on both)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", owner)


def _day_to_unix(day: date) -> int:
    """UTC-midnight unix timestamp for a calendar date -- Yahoo's chart API
    takes period1/period2 as unix seconds, but a `datetime.date` (from Yahoo's
    own response or `datetime.now().date()`) has no timezone/time-of-day of
    its own to convert directly."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


class PortfolioPlugin:
    """Per-user holdings/watchlist price tracking (Yahoo, keyless) into InfluxDB,
    tagged with the owning user's own owner_id -- genuinely private data."""

    # Where this plugin connects (contract-v2 EGRESS declaration).
    EGRESS = ["query1.finance.yahoo.com"]

    PRIVATE_PER_USER = True
    # The time-series measurement this plugin writes (its platform scope).
    TIMESERIES = {"measurements": ["portfolio_holding"]}

    # The platform's time-series handle (bind_timeseries); None until bound.
    _timeseries: Any = None

    # Owner-scoped read-back (#1035). In ACTIONS (not READ_ONLY_ACTIONS) so it is
    # reached via the JWT-gated POST route -- this is PRIVATE data, never the
    # unauthenticated GET path. plugin-registry injects the caller's own owner_id.
    ACTIONS = frozenset({"get_value"})

    AI_TOOLS = [
        {
            "name": "get_portfolio_value",
            "description": (
                "Return the latest recorded price for each symbol the calling user "
                "personally tracks in their private portfolio/watchlist. Reads only "
                "the caller's own holdings."
            ),
            # No parameters: the caller's identity (owner_id) is injected server-side
            # from the JWT, never supplied by the model/caller.
            "parameters": {"type": "object", "properties": {}, "required": []},
            "action": "get_value",
        },
    ]

    CONFIG_SCHEMA = [
        {
            "key": "PORTFOLIO_HOLDINGS",
            "type": "string",
            "default": "",
            "description": (
                "Yahoo symbols you personally hold/watch, comma-separated "
                "(e.g. AAPL,BTC-USD,TSLA). Empty = nothing tracked for you."
            ),
        },
        {
            "key": "PORTFOLIO_SINK_INFLUXDB",
            "type": "bool",
            "default": True,
            "description": "Write your holdings' price series to InfluxDB.",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.http_timeout = 20.0
        self.status = "registered"
        self._last: Dict = {}
        self.symbols: List[str] = []
        self.sink_influxdb = True
        self._owner_id: Optional[str] = None
        # owner tag -> symbol -> {"price", "as_of"}: the last point written for
        # each owner (what get_value returns). Keyed by owner, so one owner's
        # call never sees another's holdings.
        self._recorded: Dict[str, Dict[str, Dict]] = {}

    def apply_config(self, cfg: Dict) -> None:
        """Map centrally-managed per-user config -> runtime state. ``_owner_id``
        is not a CONFIG_SCHEMA field -- it's injected only by monitoring.py's
        ``_collect_private_plugin_data``, never settable via the config API."""
        if "PORTFOLIO_HOLDINGS" in cfg:
            self.symbols = [
                s.strip().upper()
                for s in str(cfg["PORTFOLIO_HOLDINGS"] or "").split(",")
                if s.strip()
            ]
        if "PORTFOLIO_SINK_INFLUXDB" in cfg:
            v = cfg["PORTFOLIO_SINK_INFLUXDB"]
            self.sink_influxdb = (
                v
                if isinstance(v, bool)
                else str(v).lower() in ("1", "true", "yes", "on")
            )
        if "_owner_id" in cfg:
            self._owner_id = cfg["_owner_id"]

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="portfolio",
            version="1.0.0",
            description="Per-user holdings/watchlist price tracking (Yahoo, keyless) to InfluxDB.",
            author="Minder <core@minder.local>",
            capabilities=["collect", "analyze", "prices", "per-user"],
            data_sources=["yahoo-finance"],
            databases=["influxdb"],
        )

    async def initialize(self) -> None:
        self.status = "ready"

    async def health_check(self) -> Dict:
        # MUST return {"healthy": <bool>} -- the monitoring loop reads health["healthy"].
        return {"healthy": True, "sink_influxdb": self.sink_influxdb}

    async def shutdown(self) -> None:
        self.status = "shutdown"

    # ── Yahoo Finance latest close (same public, keyless endpoint crypto uses) ──
    async def _fetch_latest_close(self, symbol: str) -> Optional[Tuple[int, float]]:
        """Return (unix_seconds, close) for the most recent trading day, or
        None on any error/missing data. Never raises."""
        if not _SAFE_SYMBOL.match(symbol):
            logger.warning(f"⚠️ Refusing Yahoo fetch for unsafe symbol: {symbol!r}")
            return None
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=7)  # a week of headroom for weekends/holidays
        p1 = _day_to_unix(start)
        p2 = _day_to_unix(today) + 86400
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
            return None
        if not res or not isinstance(res[0], dict):
            return None
        ts = res[0].get("timestamp") or []
        quote = (res[0].get("indicators") or {}).get("quote") or [{}]
        closes = quote[0].get("close") or []
        for t, c in zip(reversed(ts), reversed(closes)):
            if isinstance(c, (int, float)):
                day = datetime.fromtimestamp(t, tz=timezone.utc).date()
                return (_day_to_unix(day), float(c))
        return None

    # ── time series (owner-scoped write -- the actual novel part of this plugin) ──
    def bind_timeseries(self, handle: Any) -> None:
        """Called by the host at load with this plugin's time-series handle
        (``write(lines, precision)``, scoped to ``TIMESERIES``). The platform
        tags every point with the owner it is collecting for."""
        self._timeseries = handle

    async def _write_holding(self, symbol: str, ts: int, close: float) -> bool:
        """Write one point for the current owner. Refuses (logs, returns False)
        if no owner_id is set -- e.g. a direct collect_data() call outside the
        per-owner scheduler path; the platform would refuse it too, since a
        private plugin's write needs an owner."""
        sink = self._timeseries if self.sink_influxdb else None
        if sink is None:
            return False
        if not self._owner_id:
            logger.warning(
                f"⚠️ Refusing to write portfolio data for {symbol} with no owner_id set "
                "(collect_data() called outside the per-owner scheduler path?)"
            )
            return False
        if not _SAFE_SYMBOL.match(symbol):
            logger.warning(f"⚠️ Skipping write for unsafe symbol: {symbol!r}")
            return False
        line = line_protocol(_MEASUREMENT, {"symbol": symbol}, {"price": close}, ts)
        if not line:
            return False
        try:
            await sink.write([line], "s")
        except Exception as e:
            logger.warning(
                f"⚠️ time-series write failed for {symbol}: {type(e).__name__}"
            )
            return False
        as_of = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        self._recorded.setdefault(_owner_tag(self._owner_id), {})[symbol] = {
            "price": close,
            "as_of": as_of,
        }
        return True

    # ── registry-driven reads ────────────────────────────────────────────────
    async def collect_data(self) -> Dict:
        """Fetch + write the current owner's configured holdings' latest close,
        tagged with their own owner_id. Called once per real owner by
        monitoring.py's _collect_private_plugin_data, which sets self._owner_id
        (via apply_config) immediately before each call."""
        result: Dict[str, Dict] = {}
        for symbol in self.symbols:
            point = await self._fetch_latest_close(symbol)
            if point is None:
                result[symbol] = {"written": False, "error": "price unavailable"}
                continue
            ts, close = point
            wrote = await self._write_holding(symbol, ts, close)
            result[symbol] = {"written": wrote, "latest_close": close}
        self._last = {
            "owner_id": self._owner_id,
            "symbols": result,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        written = sum(1 for r in result.values() if r.get("written"))
        logger.info(
            f"📊 portfolio collect [owner {self._owner_id}]: "
            f"{written}/{len(result)} symbol(s) written"
        )
        return self._last

    async def analyze(self) -> Dict:
        """Return the most recent collection summary (whichever owner was
        collected last -- this reflects shared-instance runtime state, same
        caveat as every other plugin's analyze())."""
        if not self._last:
            return {"message": "no data collected yet"}
        return self._last

    # ── owner-scoped read-back action (#1035) ────────────────────────────────
    async def get_value(self, owner_id: str) -> Dict:
        """Latest recorded price for each symbol THIS owner tracks: the last
        point written for them (see the module docstring).

        ``owner_id`` is injected by plugin-registry's action dispatch from the
        authenticated caller's JWT (PRIVATE_PER_USER, never caller-supplied), so a
        user only ever sees their own holdings. Read-only."""
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("owner_id is required")
        holdings = {
            sym: dict(point)
            for sym, point in self._recorded.get(_owner_tag(owner), {}).items()
        }
        result: Dict = {
            "owner_id": owner,
            "holdings": holdings,
            "symbol_count": len(holdings),
        }
        if not holdings:
            result["message"] = "no price recorded for you since the plugin started"
        return result
