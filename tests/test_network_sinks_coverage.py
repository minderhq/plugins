"""Unit tests filling network plugin's sink coverage gaps (this repo's network/,
76% coverage after test_network_plugin_lifecycle_coverage.py -- this file's
follow-up, covering the four fan-out sinks: _sink_telegraf/_sink_postgres/
_sink_neo4j/_sink_rabbitmq (~180 lines, previously almost entirely untested).

test_network_plugin.py's test_reconcile_fans_out_to_all_enabled_sinks mocks
every sink out entirely (never exercising a sink's own body), and
test_sink_postgres_connect_failure_omits_raw_message covers exactly one
branch of one sink. This adds the rest: each sink's success path, its
"nothing to do" short-circuit, its HTTP/DB-error branch, and -- per the
module's own documented security contract ("sink error logs record the
exception TYPE only, never the message, to avoid leaking secrets") -- that
no sink's error response ever contains the raw exception message.

httpx.AsyncClient is monkeypatched to build a REAL client over an
httpx.MockTransport per test (matches test_registry_bundles_orchestration.py's
_ContainerOps convention) so _sink_neo4j/_sink_rabbitmq exercise real
request/response plumbing without a live server. asyncpg.connect is
monkeypatched directly (matches the existing postgres-connect-failure test's
own convention).
"""

from unittest.mock import AsyncMock

import httpx
import pytest
from minder_plugin_sdk import (
    CapabilityDenied,
    CapabilityUnavailable,
    FakePlatformContext,
)
from minder_plugin_sdk.context import CAP_PLUGINS_INVOKE

from network import NetworkPlugin, _telegraf_config

_LIVE_HOST = {
    "host": "10.0.0.5",
    "hostname": "router",
    "state": "up",
    "ports": [{"port": 22, "protocol": "tcp"}],
    "snmp": {"system": {"sysDescr": "Linux router"}, "interfaces": []},
}

# The least-privilege handle plugin-registry passes in.
_SCOPED_DB = {
    "host": "postgres",
    "port": 5432,
    "user": "minder_plugins",
    "password": "plugin-pw",
    "database": "minder",
    "schema": "plugin_data",
}


# The neo4j / rabbitmq handles the host passes in process (never the runner).
_NEO4J = {"url": "http://neo4j:7474", "user": "neo4j", "password": "test"}
_RABBITMQ = {"url": "http://rabbitmq:15672", "user": "minder", "password": "mq-pw"}


_RealAsyncClient = httpx.AsyncClient


def _mock_client(handler):
    return lambda **kwargs: _RealAsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://fake"
    )


# ── _sink_telegraf ────────────────────────────────────────────────────────────


def _bound(handler=None, **ctx_kwargs):
    """A NetworkPlugin bound to a fake platform context whose
    plugins.invoke("telegraf", "set_managed_region") runs ``handler``."""
    pl = NetworkPlugin({})
    calls = []

    async def _default(**kwargs):
        calls.append(kwargs)
        return {"written": True}

    ctx = FakePlatformContext.for_plugin(
        pl,
        dependencies=["telegraf"],
        invoke_results={("telegraf", "set_managed_region"): handler or _default},
        **ctx_kwargs,
    )
    pl.bind_context(ctx)
    return pl, ctx, calls


@pytest.mark.asyncio
async def test_sink_telegraf_unavailable_without_a_platform_context():
    # Unbound (no host context): no plugins.invoke, so nothing is attempted.
    result = await NetworkPlugin({})._sink_telegraf([_LIVE_HOST])

    assert result == {"status": "unavailable"}


@pytest.mark.asyncio
async def test_sink_telegraf_unavailable_when_invoke_is_not_granted():
    pl, ctx, calls = _bound(denied={CAP_PLUGINS_INVOKE})

    result = await pl._sink_telegraf([_LIVE_HOST])

    assert result == {"status": "unavailable"}
    assert ctx.calls == [] and calls == []


@pytest.mark.asyncio
async def test_sink_telegraf_unchanged_when_config_is_identical():
    pl, ctx, calls = _bound()
    pl._applied_cfg = _telegraf_config([_LIVE_HOST], pl.snmp_community)

    result = await pl._sink_telegraf([_LIVE_HOST])

    assert result == {"status": "unchanged"}
    assert ctx.calls == [] and calls == []


@pytest.mark.asyncio
async def test_sink_telegraf_applies_through_plugins_invoke():
    pl, ctx, calls = _bound()

    result = await pl._sink_telegraf([_LIVE_HOST])

    expected_cfg = _telegraf_config([_LIVE_HOST], pl.snmp_community)
    assert result == {"status": "applied", "bytes": len(expected_cfg)}
    assert pl._applied_cfg == expected_cfg
    assert calls == [{"body": expected_cfg, "reload": True}]
    (call,) = ctx.called(CAP_PLUGINS_INVOKE)
    assert call.args["plugin"] == "telegraf"
    assert call.args["action"] == "set_managed_region"


@pytest.mark.asyncio
async def test_sink_telegraf_error_omits_raw_message():
    async def boom(**_kwargs):
        raise RuntimeError("secret config path leaked here")

    pl, _, _ = _bound(boom)

    result = await pl._sink_telegraf([_LIVE_HOST])

    assert result == {"status": "error", "error": "RuntimeError"}
    assert "secret config path" not in str(result)
    assert pl._applied_cfg == ""  # retried on the next cycle


@pytest.mark.asyncio
async def test_sink_telegraf_reports_a_refused_call_as_an_error():
    async def refused(**_kwargs):
        raise CapabilityDenied(CAP_PLUGINS_INVOKE, "not a declared dependency edge")

    pl, _, _ = _bound(refused)

    result = await pl._sink_telegraf([_LIVE_HOST])

    assert result == {"status": "error", "error": "CapabilityDenied"}


@pytest.mark.asyncio
async def test_sink_telegraf_reports_not_loaded_when_telegraf_is_unavailable():
    async def unavailable(**_kwargs):
        raise CapabilityUnavailable(
            CAP_PLUGINS_INVOKE, "plugin 'telegraf' is not running"
        )

    pl, _, _ = _bound(unavailable)

    assert await pl._sink_telegraf([_LIVE_HOST]) == {"status": "telegraf-not-loaded"}
    assert pl._applied_cfg == ""


@pytest.mark.asyncio
async def test_sink_telegraf_reports_a_truncated_region():
    hosts = [
        {
            "host": f"10.0.0.{i}",
            "state": "up",
            "ports": [{"port": p, "protocol": "tcp"} for p in range(1, 9)],
            "snmp": {"system": {}},
        }
        for i in range(256)
    ]
    pl, _, calls = _bound()

    result = await pl._sink_telegraf(hosts)

    assert result["status"] == "truncated"
    assert result["hosts_dropped"] > 0
    assert len(calls[0]["body"].encode("utf-8")) <= 64 * 1024
    # Same scan again: nothing to rewrite, but the truncation stays visible.
    again = await pl._sink_telegraf(hosts)
    assert again["status"] == "unchanged"
    assert again["hosts_dropped"] == result["hosts_dropped"]


def test_network_declares_the_telegraf_edge_and_lan_egress():
    import asyncio

    pl = NetworkPlugin({})
    assert pl.CONTRACT_VERSION == 2
    assert pl.EGRESS == "lan"
    assert asyncio.run(pl.register()).dependencies == ["telegraf"]


# ── _sink_postgres ─────────────────────────────────────────────────────────────


class _FakePgConnection:
    def __init__(self, execute_results=None, execute_error=None):
        self.executed = []
        self.closed = False
        self._execute_results = execute_results or {}
        self._execute_error = execute_error

    async def execute(self, query, *args):
        if self._execute_error:
            raise self._execute_error
        self.executed.append((query, args))
        for marker, result in self._execute_results.items():
            if marker in query:
                return result
        return ""

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_sink_postgres_success_reports_upserted_marked_down_and_purged(
    monkeypatch,
):
    import asyncpg

    conn = _FakePgConnection(
        execute_results={
            "UPDATE network_inventory": "UPDATE 3",
            "DELETE FROM network_inventory": "DELETE 2",
        }
    )
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=conn))
    pl = NetworkPlugin({"database": _SCOPED_DB})

    result = await pl._sink_postgres(
        [_LIVE_HOST], {"new": ["10.0.0.5"], "down": [], "changed": []}
    )

    assert result == {
        "status": "ok",
        "upserted": 1,
        "marked_down": 3,
        "purged": 2,
    }
    assert conn.closed is True


@pytest.mark.asyncio
async def test_sink_postgres_closes_connection_even_when_execute_fails(monkeypatch):
    import asyncpg

    conn = _FakePgConnection(execute_error=RuntimeError("disk full, password=hunter2"))
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=conn))
    pl = NetworkPlugin({"database": _SCOPED_DB})

    result = await pl._sink_postgres([_LIVE_HOST], {})

    assert result == {"status": "error", "error": "RuntimeError"}
    assert "hunter2" not in str(result)
    assert conn.closed is True


@pytest.mark.asyncio
async def test_sink_postgres_connects_with_the_scoped_handle_not_the_env(
    monkeypatch,
):
    """The sink uses the registry-passed least-privilege
    handle (and its schema), never the owner POSTGRES_* credentials in the env."""
    import asyncpg

    monkeypatch.setenv("POSTGRES_USER", "minder")
    monkeypatch.setenv("POSTGRES_PASSWORD", "owner-secret")
    connect = AsyncMock(return_value=_FakePgConnection())
    monkeypatch.setattr(asyncpg, "connect", connect)
    pl = NetworkPlugin({"database": _SCOPED_DB})

    await pl._sink_postgres([_LIVE_HOST], {})

    kwargs = connect.await_args.kwargs
    assert kwargs["user"] == "minder_plugins"
    assert kwargs["password"] == "plugin-pw"
    assert kwargs["server_settings"] == {"search_path": "plugin_data"}
    assert "owner-secret" not in str(kwargs)


@pytest.mark.asyncio
async def test_sink_postgres_without_a_database_handle_skips(monkeypatch):
    """No handle from the registry -> no connection at all (fail closed), even
    with owner credentials present in the process env."""
    import asyncpg

    monkeypatch.setenv("POSTGRES_PASSWORD", "owner-secret")
    connect = AsyncMock()
    monkeypatch.setattr(asyncpg, "connect", connect)

    result = await NetworkPlugin({})._sink_postgres([_LIVE_HOST], {})

    assert result == {"status": "no-database"}
    connect.assert_not_awaited()


# ── _sink_neo4j ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_neo4j_no_auth_without_a_neo4j_handle(monkeypatch):
    """No handle -> skipped, even with the platform credential in the env (a
    plugin never reads it from there)."""
    monkeypatch.setenv("NEO4J_AUTH", "neo4j/owner-secret")
    client = AsyncMock()
    monkeypatch.setattr(httpx, "AsyncClient", client)

    assert await NetworkPlugin({})._sink_neo4j([_LIVE_HOST]) == {"status": "no-auth"}
    partial = NetworkPlugin({"neo4j": {"user": "neo4j"}})
    assert await partial._sink_neo4j([_LIVE_HOST]) == {"status": "no-auth"}
    client.assert_not_called()


@pytest.mark.asyncio
async def test_sink_neo4j_uses_the_handle_url_and_credentials(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"errors": []})

    def client(**kwargs):
        seen["kwargs_auth"] = kwargs.get("auth")
        return _RealAsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    pl = NetworkPlugin({"neo4j": dict(_NEO4J, url="http://graph:7474/")})

    result = await pl._sink_neo4j([_LIVE_HOST])

    assert result["status"] == "ok"
    assert seen["url"] == "http://graph:7474/db/neo4j/tx/commit"
    assert seen["kwargs_auth"] == ("neo4j", "test")


@pytest.mark.asyncio
async def test_sink_neo4j_success_with_no_cypher_errors(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"errors": []})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"neo4j": _NEO4J})

    result = await pl._sink_neo4j([_LIVE_HOST])

    assert result == {"status": "ok", "hosts": 1, "errors": []}


@pytest.mark.asyncio
async def test_sink_neo4j_reports_cypher_errors(monkeypatch):
    def handler(request):
        return httpx.Response(
            200, json={"errors": [{"message": "constraint violated"}]}
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"neo4j": _NEO4J})

    result = await pl._sink_neo4j([_LIVE_HOST])

    assert result["status"] == "error"
    assert result["errors"] == [{"message": "constraint violated"}]


@pytest.mark.asyncio
async def test_sink_neo4j_non_200_http_status(monkeypatch):
    def handler(request):
        return httpx.Response(500, text="internal error")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"neo4j": _NEO4J})

    result = await pl._sink_neo4j([_LIVE_HOST])

    assert result == {"status": "error", "http_status": 500, "hosts": 1}


@pytest.mark.asyncio
async def test_sink_neo4j_non_json_response(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="not json at all")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"neo4j": _NEO4J})

    result = await pl._sink_neo4j([_LIVE_HOST])

    assert result == {"status": "error", "error": "non-JSON response", "hosts": 1}


@pytest.mark.asyncio
async def test_sink_neo4j_transport_error_omits_raw_message(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("neo4j:7474 unreachable, auth=secret", request=request)

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"neo4j": _NEO4J})

    result = await pl._sink_neo4j([_LIVE_HOST])

    assert result == {"status": "error", "error": "ConnectError"}
    assert "secret" not in str(result)


# ── _sink_rabbitmq ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_rabbitmq_no_events_short_circuits():
    pl = NetworkPlugin({})

    result = await pl._sink_rabbitmq({"new": [], "down": [], "changed": []})

    assert result == {"status": "no-events"}


@pytest.mark.asyncio
async def test_sink_rabbitmq_no_auth_without_a_rabbitmq_handle(monkeypatch):
    monkeypatch.setenv("RABBITMQ_PASSWORD", "owner-secret")
    client = AsyncMock()
    monkeypatch.setattr(httpx, "AsyncClient", client)

    result = await NetworkPlugin({})._sink_rabbitmq(
        {"new": ["10.0.0.5"], "down": [], "changed": []}
    )

    assert result == {"status": "no-auth"}
    client.assert_not_called()


@pytest.mark.asyncio
async def test_sink_rabbitmq_success_counts_sent_and_routed(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"routed": True})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"rabbitmq": _RABBITMQ})

    result = await pl._sink_rabbitmq({"new": ["10.0.0.5"], "down": [], "changed": []})

    assert result == {
        "status": "ok",
        "events": 1,
        "sent": 1,
        "routed": 1,
        "errors": 0,
    }


@pytest.mark.asyncio
async def test_sink_rabbitmq_partial_status_on_http_failures(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"routed": True})
        return httpx.Response(500, text="exchange not found")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"rabbitmq": _RABBITMQ})

    result = await pl._sink_rabbitmq(
        {"new": ["10.0.0.5"], "down": ["10.0.0.6"], "changed": []}
    )

    assert result["status"] == "partial"
    assert result["events"] == 2
    assert result["sent"] == 1
    assert result["errors"] == 1


@pytest.mark.asyncio
async def test_sink_rabbitmq_tolerates_non_json_response(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="not json")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"rabbitmq": _RABBITMQ})

    result = await pl._sink_rabbitmq({"new": ["10.0.0.5"], "down": [], "changed": []})

    assert result["status"] == "ok"
    assert result["sent"] == 1
    assert result["routed"] == 0  # non-JSON body -> "routed" can't be confirmed


@pytest.mark.asyncio
async def test_sink_rabbitmq_transport_error_omits_raw_message(monkeypatch):
    def handler(request):
        raise httpx.ConnectError(
            "rabbitmq:15672 auth=hunter2 unreachable", request=request
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    pl = NetworkPlugin({"rabbitmq": _RABBITMQ})

    result = await pl._sink_rabbitmq({"new": ["10.0.0.5"], "down": [], "changed": []})

    assert result == {"status": "error", "error": "ConnectError"}
    assert "hunter2" not in str(result)
