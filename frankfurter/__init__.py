"""Frankfurter — foreign-exchange rates (first-party, community catalog).

Polls the public, **keyless** Frankfurter API (ECB reference rates) for the
configured base + symbols and can sink them to InfluxDB. Exposes a ``convert``
AI tool the LLM can call. A worked, self-contained catalog plugin: config UI,
an action + AI tool, a DISPLAY, and declared REQUIRES.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from minder_plugin_sdk import PluginBase, PluginMetadata, line_protocol

__all__ = ["FrankfurterPlugin"]

logger = logging.getLogger("minder.plugin.frankfurter")

# The API moved from api.frankfurter.app to api.frankfurter.dev/v1; the old host
# now answers 301. Redirects are deliberately NOT followed (the declared EGRESS
# host is the only place this plugin may connect), so call the new base directly.
_API = "https://api.frankfurter.dev/v1"


class FrankfurterPlugin(PluginBase):
    # Where this plugin connects (contract-v2 EGRESS declaration).
    EGRESS = ["api.frankfurter.dev"]
    # The time-series measurement this plugin writes (its platform scope).
    TIMESERIES = {"measurements": ["fx_rate"]}

    # The platform's time-series handle (bind_timeseries); None until bound.
    _timeseries: Any = None

    DISPLAY = {
        "label": "Exchange Rates",
        "summary": "ECB foreign-exchange rates as a time series, plus a convert tool.",
        "logo": "banknote",
        "color": "#22c55e",
        "category": "data-source",
    }

    # Optional InfluxDB sink; works fine without it (rates still fetched/served).
    REQUIRES = {"services": [], "optional_services": ["influxdb"], "bundles": []}

    ACTIONS = frozenset({"refresh", "convert"})
    READ_ONLY_ACTIONS = frozenset({"convert"})

    CONFIG_SCHEMA = [
        {
            "key": "FX_BASE",
            "type": "string",
            "default": "EUR",
            "description": "Base currency (ISO 4217, e.g. EUR, USD, TRY).",
            "widget": "text",
            "group": "Rates",
        },
        {
            "key": "FX_SYMBOLS",
            "type": "string",
            "default": "USD,GBP,TRY,JPY",
            "description": "Comma-separated target currencies to track.",
            "widget": "textarea",
            "rows": 2,
            "group": "Rates",
        },
        {
            "key": "FX_SINK_INFLUXDB",
            "type": "bool",
            "default": True,
            "description": "Write each collection to InfluxDB as a time series.",
            "widget": "toggle",
            "group": "Storage",
        },
    ]

    AI_TOOLS = [
        {
            "name": "convert",
            "description": "Convert an amount from one currency to another (live rate).",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "from_currency": {"type": "string"},
                    "to_currency": {"type": "string"},
                },
                "required": ["amount", "from_currency", "to_currency"],
            },
            "action": "convert",
            "method": "GET",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.http_timeout = float(os.environ.get("FX_HTTP_TIMEOUT", "10"))
        super().__init__(config)

    def apply_config(self, cfg: Dict) -> None:
        if "FX_BASE" in cfg:
            self.base = (cfg["FX_BASE"] or "EUR").strip().upper()
        if "FX_SYMBOLS" in cfg:
            self.symbols = [
                s.strip().upper()
                for s in (cfg["FX_SYMBOLS"] or "").split(",")
                if s.strip()
            ]
        if "FX_SINK_INFLUXDB" in cfg:
            v = cfg["FX_SINK_INFLUXDB"]
            self.sink_influxdb = (
                v
                if isinstance(v, bool)
                else str(v).lower() in ("1", "true", "yes", "on")
            )

    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="frankfurter",
            version="1.0.1",
            description="Keyless ECB foreign-exchange rates + a convert tool.",
            author="minderhq",
            capabilities=["collect", "analyze", "fx"],
            data_sources=["frankfurter"],
            databases=["influxdb"],
        )

    async def _latest(self, base: str, symbols: List[str]) -> Optional[Dict]:
        params = {"from": base}
        if symbols:
            params["to"] = ",".join(symbols)
        try:
            async with httpx.AsyncClient(
                timeout=self.http_timeout, follow_redirects=False
            ) as client:
                resp = await client.get(f"{_API}/latest", params=params)
                resp.raise_for_status()
                body = resp.json() or {}
        except Exception as e:
            logger.warning(f"fx fetch failed: {type(e).__name__}: {e}")
            return None
        rates = body.get("rates")
        return rates if isinstance(rates, dict) else None

    def bind_timeseries(self, handle: Any) -> None:
        """Called by the host at load with this plugin's time-series handle
        (``write(lines, precision)``, scoped to ``TIMESERIES``). The plugin holds
        no InfluxDB address or token; with no handle bound the sink is skipped."""
        self._timeseries = handle

    async def _write_influxdb(self, base: str, rates: Dict) -> bool:
        """Write each rate as a point (measurement 'fx_rate', tags base+quote,
        float field 'rate'). The advertised sink used to be a no-op."""
        sink = self._timeseries if self.sink_influxdb else None
        if not (sink is not None and rates):
            return False
        lines = [
            ln
            for quote, rate in rates.items()
            if isinstance(rate, (int, float))
            and not isinstance(rate, bool)
            and (
                ln := line_protocol(
                    "fx_rate", {"base": base, "quote": quote}, {"rate": float(rate)}
                )
            )
        ]
        if not lines:
            return False
        try:
            await sink.write(lines, "s")
            return True
        except Exception as e:
            logger.warning(f"⚠️ time-series write failed: {type(e).__name__}")
            return False

    async def collect_data(self) -> Dict:
        rates = await self._latest(self.base, self.symbols)
        wrote = await self._write_influxdb(self.base, rates or {})
        self._last = {
            "base": self.base,
            "rates": rates or {},
            "influxdb_written": wrote,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"fx collect: base={self.base} rates={len(rates or {})}")
        return self._last

    async def refresh(self) -> Dict:
        """Force an immediate re-collection (same as the hourly loop)."""
        return await self.collect_data()

    async def convert(
        self, amount: float, from_currency: str, to_currency: str
    ) -> Dict:
        frm, to = from_currency.strip().upper(), to_currency.strip().upper()
        if frm == to:
            return {"amount": amount, "from": frm, "to": to, "result": amount}
        rates = await self._latest(frm, [to])
        if not rates or to not in rates:
            return {"error": f"no rate for {frm}->{to}"}
        try:
            result = float(amount) * float(rates[to])
        except (TypeError, ValueError):
            return {"error": "amount must be a number"}
        return {"amount": amount, "from": frm, "to": to, "result": result}
