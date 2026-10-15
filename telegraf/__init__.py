"""Telegraf config-manager plugin (first-party module plugin).

Responsibility: manage the **plugin-managed region** of the telegraf
configuration and get telegraf onto the new config. The region sits between
two markers in the runtime ``telegraf.conf``; the rest of the hand-maintained
config is never touched.

Contract v2 (platform context): this plugin declares ``MANAGES =
["telegraf"]`` and reaches telegraf only through ``self.ctx.telegraf.*``. It
never opens the config file and never talks to Docker. The platform:

* validates the region (allowlisted ``[[inputs.*]]`` types only, no
  environment substitution, size caps) and that the merged file still parses;
* writes it atomically (temp file + rename in the config's directory);
* applies it: telegraf runs with ``--watch-config=poll`` and picks the write up
  without a restart. ``reload(force_restart=True)`` asks the platform for a
  container restart, a rare fallback the platform audit-logs.

The same code works when the plugin runs inside the plugin host process and
when it runs in a separate runner: only the platform context differs.

This plugin does NOT do network discovery. The ``network`` plugin feeds
discovered targets into this plugin's managed region through
``ctx.plugins.invoke("telegraf", "set_managed_region", ...)``.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from minder_plugin_sdk import UNBOUND_CONTEXT, CapabilityInvalid, PluginMetadata
from minder_plugin_sdk.context import (
    CAP_TELEGRAF_SET_MANAGED_REGION,
    PlatformContext,
)

__all__ = ["TelegrafPlugin"]

logger = logging.getLogger("minder.plugin.telegraf")


class TelegrafPlugin:
    """Manages the telegraf config's managed region and reloads telegraf."""

    # Contract v2: the platform binds self.ctx and grants telegraf.* because of
    # MANAGES. No outbound network access of its own (EGRESS: none).
    CONTRACT_VERSION = 2
    MANAGES = ["telegraf"]
    EGRESS: List[str] = []

    # Methods invokable via POST /v1/plugins/telegraf/actions/<name> (JWT-gated).
    # Whitelist only — nothing else on the instance is reachable over HTTP.
    ACTIONS = frozenset({"set_managed_region", "clear_managed_region", "reload"})
    # Admin-only subset: these two rewrite telegraf's live config. The platform
    # validates the content (allowlisted input types only, so e.g. inputs.exec
    # is refused), but changing what telegraf monitors stays an admin
    # decision. `reload` alone is a no-op unless `force_restart=True`, and even
    # then only restarts a container.
    ADMIN_ONLY_ACTIONS = frozenset({"set_managed_region", "clear_managed_region"})

    # Class-level default so the attribute exists before the host binds one.
    ctx: PlatformContext = UNBOUND_CONTEXT

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.status = "registered"
        # Serialise write+reload so concurrent action calls can't interleave.
        self._lock = asyncio.Lock()
        # The last region this instance wrote (None: not written since start).
        # The platform owns the file; this is only what the plugin reports.
        self._region: Optional[str] = None
        self._last_write: Optional[Dict] = None

    def bind_context(self, ctx: PlatformContext) -> None:
        """Called by the host at load with this plugin's platform context."""
        self.ctx = ctx

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="telegraf",
            version="2.0.0",
            description="Manages the telegraf config's managed region and reloads telegraf.",
            author="Minder <core@minder.local>",
            capabilities=["config-management", "reload"],
            data_sources=["telegraf-config"],
            databases=["influxdb"],
        )

    async def initialize(self) -> None:
        # Never touches the config on init: the region is only written when a
        # caller sets it.
        self.status = "ready"

    def _managed(self) -> bool:
        return self.ctx.has(CAP_TELEGRAF_SET_MANAGED_REGION)

    async def health_check(self) -> Dict:
        # monitoring.py reads health.get("healthy"). Healthy when the platform
        # grants this plugin the managed region (no grant: nothing can be set).
        managed = self._managed()
        return {"healthy": self.status == "ready" and managed, "managed": managed}

    async def collect_data(self) -> Dict:
        """Read-only: report what this plugin last wrote to the managed region."""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "managed": self._managed(),
            "region_written": self._region is not None,
            "managed_inputs": self.list_managed_inputs(),
            "last_write": self._last_write,
        }

    async def analyze(self) -> Dict:
        inputs = self.list_managed_inputs()
        return {"managed_input_count": len(inputs), "managed_inputs": inputs}

    async def shutdown(self) -> None:
        self.status = "shutdown"

    # ── config management ────────────────────────────────────────────────────
    def list_managed_inputs(self) -> List[str]:
        """Names of the `[[inputs.X]]` blocks in the region this instance last
        wrote (empty until it writes one)."""
        names = []
        for line in (self._region or "").splitlines():
            s = line.strip()
            if s.startswith("[[inputs.") and s.endswith("]]"):
                names.append(s[len("[[inputs.") : -2])
        return names

    async def set_managed_region(self, body: str, reload: bool = True) -> Dict:
        """Replace everything between the markers with ``body``.

        The platform validates ``body`` before writing it, so a malformed or
        disallowed region never reaches telegraf; a rejection raises
        ``ValueError`` (a 400 for an action call) naming the rule it broke.
        """
        if not isinstance(body, str):
            raise ValueError("managed region body must be a string")
        body = body.strip("\n")
        async with self._lock:
            try:
                written = await self.ctx.telegraf.set_managed_region(body)
            except CapabilityInvalid as e:
                raise ValueError(str(e)) from None
            self._region = body
            result: Dict = {"written": True, **(written or {})}
            if reload:
                result["reload"] = await self.reload()
            self._last_write = {
                "at": datetime.now(timezone.utc).isoformat(),
                "bytes": len(body.encode("utf-8")),
                "inputs": len(self.list_managed_inputs()),
            }
        logger.info(
            "telegraf managed region updated (%d bytes, %d input(s))",
            self._last_write["bytes"],
            self._last_write["inputs"],
        )
        return result

    async def clear_managed_region(self, reload: bool = True) -> Dict:
        return await self.set_managed_region("", reload=reload)

    # ── reload: watch-config (happy path) + restart fallback ──────────────────
    async def reload(self, force_restart: bool = False) -> Dict:
        """Get telegraf onto the new config.

        The platform's write already triggers telegraf's ``--watch-config=poll``
        reload, so the normal path needs nothing more. ``force_restart=True``
        asks the platform to restart the container (fallback for when a reload
        doesn't take).
        """
        return await self.ctx.telegraf.reload(force=bool(force_restart))
