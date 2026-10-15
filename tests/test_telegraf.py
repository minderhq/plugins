"""Unit tests for the telegraf config-manager plugin (this repo's telegraf/).

Contract v2: the plugin writes the managed region only through
``self.ctx.telegraf.*``, so these tests bind the SDK's FakePlatformContext. No
file and no Docker are involved.
"""

import asyncio

import pytest
from minder_plugin_sdk import (
    CapabilityDenied,
    CapabilityInvalid,
    CapabilityUnavailable,
    FakePlatformContext,
    declaration_errors,
)
from minder_plugin_sdk.context import (
    CAP_TELEGRAF_RELOAD,
    CAP_TELEGRAF_SET_MANAGED_REGION,
)

from telegraf import TelegrafPlugin

_REGION = '[[inputs.net_response]]\n  protocol = "tcp"\n  address = "10.0.0.1:22"'


def _bound(**kwargs):
    pl = TelegrafPlugin({})
    ctx = FakePlatformContext.for_plugin(pl, **kwargs)
    pl.bind_context(ctx)
    return pl, ctx


def test_declarations_are_contract_v2_and_manage_telegraf():
    pl = TelegrafPlugin({})
    assert pl.CONTRACT_VERSION == 2
    assert pl.MANAGES == ["telegraf"]
    assert pl.EGRESS == []
    assert declaration_errors(pl) == []


def test_register_and_health_when_granted():
    pl, _ = _bound()
    md = asyncio.run(pl.register())
    assert md.name == "telegraf"
    assert "config-management" in md.capabilities
    asyncio.run(pl.initialize())
    health = asyncio.run(pl.health_check())
    assert health == {"healthy": True, "managed": True}


def test_unhealthy_without_a_bound_context():
    pl = TelegrafPlugin({})
    asyncio.run(pl.initialize())
    assert asyncio.run(pl.health_check()) == {"healthy": False, "managed": False}


def test_unhealthy_before_initialize():
    pl, _ = _bound()
    assert asyncio.run(pl.health_check())["healthy"] is False


def test_set_managed_region_goes_through_the_context():
    pl, ctx = _bound()
    result = asyncio.run(pl.set_managed_region("\n" + _REGION + "\n", reload=False))
    assert result["written"] is True
    # Surrounding newlines are trimmed before the platform sees the region.
    assert ctx.telegraf_region == _REGION
    assert pl.list_managed_inputs() == ["net_response"]
    assert ctx.telegraf_reloads == []


def test_set_managed_region_reloads_by_default():
    pl, ctx = _bound()
    result = asyncio.run(pl.set_managed_region(_REGION))
    assert ctx.telegraf_reloads == [False]
    assert "reload" in result
    assert ctx.called(CAP_TELEGRAF_RELOAD)[0].args == {"force": False}


def test_platform_rejection_is_a_value_error_and_keeps_the_old_view():
    def reject(_toml):
        raise CapabilityInvalid(
            CAP_TELEGRAF_SET_MANAGED_REGION, "input type is not allowlisted"
        )

    pl, ctx = _bound()
    asyncio.run(pl.set_managed_region(_REGION, reload=False))
    ctx.telegraf_validator = reject
    with pytest.raises(ValueError, match="not allowlisted"):
        asyncio.run(pl.set_managed_region("[[inputs.exec]]", reload=False))
    assert pl.list_managed_inputs() == ["net_response"]


def test_non_string_body_is_rejected_before_the_platform():
    pl, ctx = _bound()
    with pytest.raises(ValueError):
        asyncio.run(pl.set_managed_region(123, reload=False))  # type: ignore[arg-type]
    assert ctx.calls == []


def test_denied_and_unavailable_propagate():
    pl, _ = _bound(denied={"telegraf"})
    with pytest.raises(CapabilityDenied):
        asyncio.run(pl.set_managed_region(_REGION, reload=False))
    pl, _ = _bound(unavailable={CAP_TELEGRAF_SET_MANAGED_REGION})
    with pytest.raises(CapabilityUnavailable):
        asyncio.run(pl.set_managed_region(_REGION, reload=False))
    # Unbound: the SDK's default context refuses every call.
    with pytest.raises(CapabilityUnavailable):
        asyncio.run(TelegrafPlugin({}).set_managed_region(_REGION, reload=False))


def test_clear_writes_an_empty_region():
    pl, ctx = _bound()
    asyncio.run(pl.set_managed_region(_REGION, reload=False))
    asyncio.run(pl.clear_managed_region(reload=False))
    assert ctx.telegraf_region == ""
    assert pl.list_managed_inputs() == []


def test_reload_default_and_forced():
    pl, ctx = _bound()
    asyncio.run(pl.reload())
    asyncio.run(pl.reload(force_restart=True))
    assert ctx.telegraf_reloads == [False, True]


def test_collect_and_analyze_report_the_last_write():
    pl, _ = _bound()
    data = asyncio.run(pl.collect_data())
    assert data["managed"] is True
    assert data["region_written"] is False
    assert data["managed_inputs"] == []
    asyncio.run(pl.set_managed_region(_REGION, reload=False))
    data = asyncio.run(pl.collect_data())
    assert data["region_written"] is True
    assert data["last_write"]["inputs"] == 1
    assert asyncio.run(pl.analyze()) == {
        "managed_input_count": 1,
        "managed_inputs": ["net_response"],
    }


def test_shutdown_sets_status():
    pl = TelegrafPlugin({})
    asyncio.run(pl.shutdown())
    assert pl.status == "shutdown"


def test_actions_whitelist_is_read_only_safe():
    pl = TelegrafPlugin({})
    assert pl.ACTIONS == {"set_managed_region", "clear_managed_region", "reload"}
    for name in pl.ACTIONS:
        assert callable(getattr(pl, name))


def test_config_writing_actions_are_admin_only():
    pl = TelegrafPlugin({})
    assert pl.ADMIN_ONLY_ACTIONS == {"set_managed_region", "clear_managed_region"}
    assert pl.ADMIN_ONLY_ACTIONS <= pl.ACTIONS
