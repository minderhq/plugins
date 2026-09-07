"""Unit tests for crypto/tefas_funds influx symbol/code validation.

Symbols (crypto ``CRYPTO_SYMBOLS``) and fund codes (tefas_funds
``TEFAS_FUNDS``) are API-settable config that gets interpolated into an
InfluxDB SQL query + line protocol. These lock the safe-charset guard that
stops a config value from breaking out into injection (or corrupting line
protocol with a space/comma).

Ported from minderhq/minder's tests/unit/test_plugin_influx_symbol_validation.py
(#1460 cutover) -- this repo is now the single source for these plugins.
"""

import pytest

from crypto import _SAFE_SYMBOL
from tefas_funds import _SAFE_CODE

_SAFE = ["BTC", "ETH", "BTC-USD", "AFA", "X_1.2", "a.b-c_d"]
_UNSAFE = [
    "BTC'; DROP TABLE x --",  # SQL breakout
    "sym' OR '1'='1",
    "a b",  # space breaks line protocol
    "x,y",  # comma breaks line protocol tag set
    "a=b",  # '=' breaks line protocol
    "",  # empty
    "a\nb",  # newline (multi-line injection)
    "BTC-USD\n",  # trailing newline -- `$` (not `\Z`) matches just before it
    "évil",  # non-ascii
]


@pytest.mark.parametrize("pattern", [_SAFE_SYMBOL, _SAFE_CODE])
@pytest.mark.parametrize("value", _SAFE)
def test_safe_values_accepted(pattern, value):
    assert pattern.match(value)


@pytest.mark.parametrize("pattern", [_SAFE_SYMBOL, _SAFE_CODE])
@pytest.mark.parametrize("value", _UNSAFE)
def test_injection_values_rejected(pattern, value):
    assert not pattern.match(value)
