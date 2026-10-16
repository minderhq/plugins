"""network: SNMP credentials come from self.ctx.secrets, with the environment as
the fallback (in-process hosts, unseeded secrets, calls the host cannot serve).
"""

import logging

import pytest
from minder_plugin_sdk import CapabilityDenied, FakePlatformContext
from minder_plugin_sdk.context import CAP_SECRETS_READ

import network as netmod
from network import NetworkPlugin

_ENV = {
    "NETWORK_SNMP_COMMUNITY": "env-community",
    "NETWORK_SNMP_V3_AUTH_PASS": "env-auth",
    "NETWORK_SNMP_V3_PRIV_PASS": "env-priv",
}


@pytest.fixture
def snmp_env(monkeypatch):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)


def _creds(pl):
    return (pl.snmp_community, pl.snmp_v3_auth_pass, pl.snmp_v3_priv_pass)


class _RaisingSecrets:
    def __init__(self, exc):
        self.exc = exc
        self.keys = []

    async def get(self, key):
        self.keys.append(key)
        raise self.exc


class _GrantedButFailingCtx:
    """A host that grants secrets.read but refuses the call (e.g. a background
    call without a tenant), which FakePlatformContext cannot model: its has()
    is False whenever a call would be denied."""

    def __init__(self, exc):
        self.secrets = _RaisingSecrets(exc)

    def has(self, capability):
        return capability == CAP_SECRETS_READ


async def test_secrets_override_environment(snmp_env):
    pl = NetworkPlugin({})
    ctx = FakePlatformContext.for_plugin(
        pl,
        secrets={
            "NETWORK_SNMP_COMMUNITY": "vault-community",
            "NETWORK_SNMP_V3_AUTH_PASS": "vault-auth",
            "NETWORK_SNMP_V3_PRIV_PASS": "vault-priv",
        },
    )
    pl.bind_context(ctx)

    await pl._refresh_snmp_secrets()

    assert _creds(pl) == ("vault-community", "vault-auth", "vault-priv")
    assert [c.args["key"] for c in ctx.called(CAP_SECRETS_READ)] == list(_ENV)


async def test_unset_secret_falls_back_to_environment_per_key(snmp_env):
    pl = NetworkPlugin({})
    pl.bind_context(
        FakePlatformContext.for_plugin(
            pl, secrets={"NETWORK_SNMP_COMMUNITY": "vault-community"}
        )
    )

    await pl._refresh_snmp_secrets()

    assert _creds(pl) == ("vault-community", "env-auth", "env-priv")


async def test_unbound_context_keeps_environment_values(snmp_env):
    pl = NetworkPlugin({})  # in-process host without a context: UNBOUND_CONTEXT

    await pl._refresh_snmp_secrets()

    assert _creds(pl) == ("env-community", "env-auth", "env-priv")


async def test_unbound_context_keeps_built_in_defaults(monkeypatch):
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    pl = NetworkPlugin({})

    await pl._refresh_snmp_secrets()

    assert _creds(pl) == ("public", "", "")


async def test_refused_secret_call_falls_back_without_logging_values(snmp_env, caplog):
    pl = NetworkPlugin({})
    ctx = _GrantedButFailingCtx(
        CapabilityDenied(CAP_SECRETS_READ, "this call has no tenant")
    )
    pl.bind_context(ctx)

    with caplog.at_level(logging.DEBUG, logger="minder.plugin.network"):
        await pl._refresh_snmp_secrets()

    assert _creds(pl) == ("env-community", "env-auth", "env-priv")
    assert ctx.secrets.keys == list(_ENV)
    assert "CapabilityDenied" in caplog.text
    for value in _ENV.values():
        assert value not in caplog.text


async def test_store_error_does_not_fail_the_refresh(snmp_env):
    pl = NetworkPlugin({})
    pl.bind_context(_GrantedButFailingCtx(RuntimeError("store down")))

    await pl._refresh_snmp_secrets()

    assert _creds(pl) == ("env-community", "env-auth", "env-priv")


async def test_removed_secret_reverts_to_environment(snmp_env):
    pl = NetworkPlugin({})
    ctx = FakePlatformContext.for_plugin(
        pl, secrets={"NETWORK_SNMP_COMMUNITY": "vault-community"}
    )
    pl.bind_context(ctx)
    await pl._refresh_snmp_secrets()
    assert pl.snmp_community == "vault-community"

    ctx.secrets_store.clear()
    await pl._refresh_snmp_secrets()

    assert pl.snmp_community == "env-community"


async def test_discover_reads_secrets_before_snmp(snmp_env, monkeypatch):
    monkeypatch.setenv("NETWORK_SCAN_TARGETS", "10.0.0.5")
    pl = NetworkPlugin({})
    pl.snmp_enabled = True
    pl.snmp_version = "2c"
    pl.bind_context(
        FakePlatformContext.for_plugin(
            pl, secrets={"NETWORK_SNMP_COMMUNITY": "vault-community"}
        )
    )
    monkeypatch.setattr(netmod.shutil, "which", lambda b: f"/usr/bin/{b}")

    async def fake_nmap(hosts):
        return [{"host": "10.0.0.5", "hostname": "", "state": "up", "ports": []}]

    seen = []

    async def fake_snmp_lookup(host):
        seen.append(pl._snmp_base_args())
        return {}

    monkeypatch.setattr(pl, "_nmap_scan", fake_nmap)
    monkeypatch.setattr(pl, "_snmp_lookup", fake_snmp_lookup)

    await pl._discover()

    assert seen == [["-v2c", "-c", "vault-community"]]
